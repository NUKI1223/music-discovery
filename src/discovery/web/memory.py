"""Account-local, incremental extractive memory and historical card index.

No model-generated preferences and no writes to the permanent taste profile.
Quotes retain source IDs; bounded excerpts are historical evidence, not instructions.
"""
import re

from fastapi import HTTPException

from .schemas import ChatMemory, Message, ReleaseCard


def normalize(text: str) -> str:
    return ' '.join(re.findall(r'\w+', text.casefold().replace('ё', 'е')))


def excerpt(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + '…'


def substantive(text: str) -> bool:
    value = normalize(text)
    return len(value) > 15 and value not in {
        'подбери релизы по текущим фильтрам', 'подбери релизы по моим фильтрам',
        'подборка по фильтрам', 'еще варианты в моем жанре', 'хочу более неожиданное',
    } and not value.startswith('почему альбом')


def init_memory(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS web_chat_memory (
            chat_id TEXT PRIMARY KEY REFERENCES web_chats(id),
            indexed_seq INTEGER NOT NULL DEFAULT 0,
            summary_seq INTEGER NOT NULL DEFAULT 0,
            reset_seq INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_chat_cards (
            chat_id TEXT NOT NULL, release_id TEXT NOT NULL,
            first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
            position INTEGER NOT NULL, search_text TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(chat_id,release_id)
        );
        CREATE INDEX IF NOT EXISTS web_chat_card_order ON web_chat_cards(chat_id,first_seq,position);
    ''')


def refresh_memory(store, chat_id: str) -> ChatMemory:
    with store.connection() as conn:
        init_memory(conn)
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM web_chats WHERE id=?', (chat_id,)).fetchone():
            raise HTTPException(404, 'Диалог не найден.')
        conn.execute('INSERT OR IGNORE INTO web_chat_memory(chat_id,payload) VALUES (?,?)',
                     (chat_id, ChatMemory().model_dump_json()))
        row = conn.execute('SELECT indexed_seq,summary_seq,reset_seq,payload FROM web_chat_memory WHERE chat_id=?', (chat_id,)).fetchone()
        indexed, summarized, reset_seq = row[:3]
        memory = ChatMemory.model_validate_json(row[3])
        for seq, payload in conn.execute('SELECT seq,payload FROM web_messages WHERE chat_id=? AND seq>? ORDER BY seq', (chat_id,indexed)).fetchall():
            message = Message.model_validate_json(payload)
            if message.status == 'pending':
                break  # revisit this message after completion/recovery
            if message.role == 'assistant' and message.status == 'completed':
                for position, card in enumerate(message.cards, 1):
                    conn.execute('''INSERT INTO web_chat_cards VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(chat_id,release_id) DO UPDATE SET
                        last_seq=excluded.last_seq,payload=excluded.payload''',
                        (chat_id,card.release_id,seq,seq,position,
                         normalize(card.title+' '+' '.join(card.artists)),card.model_dump_json()))
                previous = conn.execute('SELECT payload FROM web_messages WHERE chat_id=? AND seq<? ORDER BY seq DESC LIMIT 1', (chat_id,seq)).fetchone()
                if previous and seq > reset_seq:
                    user = Message.model_validate_json(previous[0])
                    if user.role == 'user' and substantive(user.text) and not message.text.startswith('Такие пожелания пока не поддерживаются:'):
                        memory.latest_request = excerpt(user.text, 800)
            indexed = seq
        # Keep the most recent six exchanges (+ current pending pair) verbatim.
        cutoff = conn.execute('SELECT seq FROM web_messages WHERE chat_id=? ORDER BY seq DESC LIMIT 1 OFFSET 14', (chat_id,)).fetchone()
        if cutoff and cutoff[0] > summarized:
            for seq, payload in conn.execute('SELECT seq,payload FROM web_messages WHERE chat_id=? AND seq>? AND seq<=? ORDER BY seq', (chat_id,max(summarized,reset_seq),cutoff[0])).fetchall():
                message = Message.model_validate_json(payload)
                if message.status == 'pending':
                    break
                memory.summarized_messages += 1
                if message.role == 'user' and substantive(message.text):
                    memory.summary.append({'message_id': message.id, 'excerpt': excerpt(message.text, 300)})
                    memory.summary = memory.summary[-6:]
                summarized = seq
        conn.execute('UPDATE web_chat_memory SET indexed_seq=?,summary_seq=?,payload=? WHERE chat_id=?',
                     (indexed,summarized,memory.model_dump_json(),chat_id))
        return memory


def update_memory(store, chat_id, notes: str, reset: bool) -> ChatMemory:
    memory = refresh_memory(store,chat_id)
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM web_messages WHERE chat_id=? AND json_extract(payload,'$.status')='pending' LIMIT 1", (chat_id,)).fetchone():
            raise HTTPException(409, 'Дождитесь завершения текущего сообщения.')
        if reset:
            last = conn.execute('SELECT coalesce(max(seq),0) FROM web_messages WHERE chat_id=?',(chat_id,)).fetchone()[0]
            memory = ChatMemory()
            conn.execute('UPDATE web_chat_memory SET reset_seq=?,summary_seq=? WHERE chat_id=?',(last,last,chat_id))
        memory.notes = notes.strip()
        conn.execute('UPDATE web_chat_memory SET payload=? WHERE chat_id=?',(memory.model_dump_json(),chat_id))
    return memory


def reset_boundary(store, chat_id) -> int:
    with store.connection() as conn:
        row = conn.execute('SELECT reset_seq FROM web_chat_memory WHERE chat_id=?',(chat_id,)).fetchone()
    return row[0] if row else 0


def shown_ids(store, chat_id) -> list[str]:
    with store.connection() as conn:
        return [row[0] for row in conn.execute('SELECT release_id FROM web_chat_cards WHERE chat_id=?',(chat_id,))]


def find_cards(store, chat_id, text: str, release_id: str | None = None) -> list[ReleaseCard]:
    query = normalize(text)
    with store.connection() as conn:
        if release_id:
            rows = conn.execute('SELECT payload FROM web_chat_cards WHERE chat_id=? AND release_id=?',(chat_id,release_id)).fetchall()
        elif re.search(r'\b(перв\w*|начал\w*)\b',query):
            rows = conn.execute('SELECT payload FROM web_chat_cards WHERE chat_id=? ORDER BY first_seq,position LIMIT 5',(chat_id,)).fetchall()
        else:
            # A bounded SQL result, even in a very large chat. Parameters cannot
            # become SQL; Unicode normalization is identical at index/query time.
            terms = list(dict.fromkeys(word for word in query.split() if len(word) >= 3 and word not in {
                'альбом','альбома','релиз','релиза','почему','объясни','расскажи','похожее','похожий','музыка','который','подходит','мне','про','тот','как','что','хочу','подбери',
            }))[:12]
            if not terms:
                return []
            conn.create_function('normalize_text',1,normalize,deterministic=True)
            partial = '+'.join('(instr(search_text,?)>0)' for _ in terms)
            exact = "(instr(?, ' ' || normalize_text(json_extract(payload,'$.title')) || ' ')>0)"
            score = f"100*{exact}+{partial}"
            parameters = (' '+query+' ', *terms)
            rows = conn.execute(f'SELECT payload FROM web_chat_cards WHERE chat_id=? AND ({score})>0 ORDER BY ({score}) DESC,last_seq DESC LIMIT 5',
                                (chat_id,*parameters,*parameters)).fetchall()

    return [ReleaseCard.model_validate_json(row[0]) for row in rows]


def latest_selection(store, chat_id) -> list[ReleaseCard]:
    with store.connection() as conn:
        rows = conn.execute('SELECT payload FROM web_chat_cards WHERE chat_id=? AND first_seq=(SELECT max(first_seq) FROM web_chat_cards WHERE chat_id=?) ORDER BY position LIMIT 5',(chat_id,chat_id)).fetchall()
    return [ReleaseCard.model_validate_json(row[0]) for row in rows]
