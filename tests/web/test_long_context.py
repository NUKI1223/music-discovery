"""Long conversations: persistence, retrieval, bounded pages and context isolation."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from discovery.catalog.demo import make_demo_catalog
from discovery.llm.client import OpenRouterSettings
from discovery.llm.context import fits_input, input_size
from discovery.storage import Store
from discovery.web.chat import (
    claim_message,
    create_chat,
    edit_memory,
    get_chat,
    handle_message,
    model_messages,
    read_chat,
)
from discovery.web.memory import find_cards, refresh_memory, shown_ids
from discovery.web.schemas import (
    ChatCreate,
    Filters,
    MemoryUpdate,
    Message,
    MessageRequest,
    ReleaseCard,
)


@pytest.fixture
def conversation(tmp_path):
    catalog = make_demo_catalog()
    store = Store(tmp_path / 'history.sqlite3')
    chat = create_chat(ChatCreate(filters=Filters()), SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog))), store)
    for i in range(100):
        user = Message(id=f'user-{i}', role='user', text='Хочу спокойную музыку для работы' if i == 0 else 'Ещё варианты', created_at='test')
        card = ReleaseCard(release_id=f'old-{i}', decision_id=f'web-assistant-{i}',
                           title=f'Archive Record {i}', artists=['Archive Artist'], year=2001,
                           genres=[], explanation=['Сохранённое объяснение.'], warnings=[], links=[], historical_ratings=[])
        assistant = Message(id=f'assistant-{i}', role='assistant', text='Вот подборка.', created_at='test', cards=[card])
        with store.connection() as conn:
            for message in (user,assistant):
                conn.execute('INSERT INTO web_messages(id,chat_id,payload) VALUES(?,?,?)',(message.id,chat.id,message.model_dump_json()))
    refresh_memory(store,chat.id)  # GET is read-only; message writes normally refresh memory
    return store,catalog,chat.id


def client(action=None):
    calls=[]
    def complete(messages,**kwargs):
        calls.append(messages)
        return {'content':json.dumps(action)}
    return SimpleNamespace(settings=OpenRouterSettings(api_key='offline-test',model='offline',
        input_usd_per_million=.075,output_usd_per_million=.2,request_cost_limit=.01,daily_cost_limit=.1,
        max_input_tokens=16384), new_budget=lambda:None, complete=complete, close=lambda:None,calls=calls)


def test_pages_are_bounded_complete_and_restart_safe(conversation):
    store,_,ident=conversation
    page=read_chat(ident,store,limit=20,before=None,after=None)
    assert len(page.messages)==20 and page.has_older and not page.has_newer
    ids=[m.id for m in page.messages]
    while page.has_older:
        page=read_chat(ident,store,limit=20,before=page.oldest_seq,after=None)
        ids=[m.id for m in page.messages]+ids
    assert len(ids)==len(set(ids))==200
    assert ids[0]=='user-0' and ids[-1]=='assistant-99'
    later=read_chat(ident,store,limit=20,after=page.newest_seq,before=None)
    assert later.messages[0].id=='user-10' and later.has_older
    reopened=get_chat(Store(store.path),ident,limit=20)
    assert reopened.memory.latest_request=='Хочу спокойную музыку для работы'
    assert reopened.memory.summarized_messages==186
    assert reopened.memory.summary[0]['message_id']=='user-0'
    assert len(shown_ids(store,ident))==100


def test_old_release_retrieved_and_explained_outside_visible_page(conversation):
    store,catalog,ident=conversation
    get_chat(store,ident,limit=20)
    assert find_cards(store,ident,'Расскажи про Archive Record 0')[0].release_id=='old-0'
    assert find_cards(store,ident,'Альбомы в начале разговора')[0].release_id=='old-0'
    llm=client({'action':'explain',**Filters().model_dump(),'count':5,'release_id':'old-0',
                'unsupported_preferences':[],'clarification':'none'})
    result=handle_message(store,ident,MessageRequest(message_id='explain-older',text='Почему этот альбом?',release_id='old-0'),catalog,lambda:llm)
    assert result.status=='completed' and result.cards[0].release_id=='old-0'
    assert 'old-0' in llm.calls[0][0]['content']
    assert 'Archive Record 0' in result.text


def test_notes_correction_reset_and_account_isolation(conversation,tmp_path):
    store,catalog,ident=conversation
    original_profile=store.profile().model_dump()
    edit_memory(ident,MemoryUpdate(notes='Сегодня хочу спокойнее. Это не постоянный вкус.'),store)
    assert get_chat(Store(store.path),ident,limit=2).memory.notes.startswith('Сегодня')
    with store.connection() as conn:
        for m in [Message(id='correction-user',role='user',text='Теперь вместо спокойной хочу агрессивную музыку',created_at='test'),Message(id='correction-answer',role='assistant',text='Уточните направление.',created_at='test')]:
            conn.execute('INSERT INTO web_messages(id,chat_id,payload) VALUES(?,?,?)',(m.id,ident,m.model_dump_json()))
    refresh_memory(store,ident)
    detail=get_chat(store,ident,limit=14)
    assert detail.memory.latest_request.startswith('Теперь вместо')
    assert store.profile().model_dump()==original_profile
    stranger=Store(tmp_path/'stranger.sqlite3')
    with pytest.raises(HTTPException) as error:
        edit_memory(ident,MemoryUpdate(notes='wrong account'),stranger)
    assert error.value.status_code==404
    edit_memory(ident,MemoryUpdate(reset=True),store)
    reset=get_chat(store,ident,limit=14,context_only=True)
    assert reset.messages==[] and reset.memory.summary==[] and reset.memory.notes==''
    assert reset.memory.latest_request==''
    assert len(shown_ids(store,ident))==100  # reset memory doesn't permit repeat recommendations
    assert len(get_chat(store,ident).messages)==202  # archive is not deleted
    body=MessageRequest(message_id='after-reset',text='Новый запрос')
    claim_message(store,ident,body)
    prompt=model_messages(get_chat(store,ident,limit=14,context_only=True),body,catalog,client())
    assert 'спокойн' not in json.dumps(prompt,ensure_ascii=False)
    with pytest.raises(HTTPException) as pending:
        edit_memory(ident,MemoryUpdate(reset=True),store)
    assert pending.value.status_code==409


def test_cyrillic_4000_and_full_recent_text_fit_without_extra_model_calls(conversation):
    store,catalog,ident=conversation
    body=MessageRequest(message_id='long-russian',text='я'*4000)
    claim_message(store,ident,body)
    llm=client()
    detail=get_chat(store,ident,limit=14,context_only=True)
    prompt=model_messages(detail,body,catalog,llm)
    assert prompt[-1]['content']==body.text
    assert len(prompt)>2
    assert fits_input({'messages':prompt},llm.settings)
    size,tokens=input_size({'messages':prompt})
    assert size>tokens  # token estimate no longer compared to raw byte count
    assert llm.calls==[]
    # Messages are no longer cut after 1,000 characters.
    detail.messages[-4].text='a'*1200+'IMPORTANT_END'
    shorter=MessageRequest(message_id='short-current',text='Что дальше?')
    prompt=model_messages(detail,shorter,catalog,llm)
    assert any('IMPORTANT_END' in m['content'] for m in prompt[1:-1])
