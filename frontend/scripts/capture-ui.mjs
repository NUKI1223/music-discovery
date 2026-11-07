import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const phase = process.argv[2] || 'after'
const data = mkdtempSync(join(tmpdir(), 'music-ui-capture-'))
const server = spawn('../.venv/bin/python', ['../scripts/web_test_server.py', '--data-dir', data, '--port','8774'], {cwd:process.cwd(),stdio:'ignore'})
const base='http://127.0.0.1:8774'
let browser
try {
 for(let i=0;i<100;i++){try {if((await fetch(base)).ok)break}catch{} await new Promise(r=>setTimeout(r,100))}
 browser=await chromium.launch({headless:true})
 const page=await browser.newPage({viewport:{width:1440,height:1000}})
 const out=`../artifacts/ui-redesign/${phase}`;mkdirSync(out,{recursive:true})
 await page.goto(base);await page.getByRole('button',{name:'Нет аккаунта? Зарегистрироваться'}).waitFor()
 await page.screenshot({path:`${out}/auth-desktop.png`,fullPage:true})
 await page.getByRole('button',{name:'Нет аккаунта? Зарегистрироваться'}).click()
 await page.getByLabel('Логин',{exact:true}).fill('visual_capture')
 await page.getByLabel('Пароль',{exact:true}).fill('capture-password-123')
 await page.getByLabel('Подтвердите пароль',{exact:true}).fill('capture-password-123')
 await page.getByRole('button',{name:'Создать аккаунт',exact:true}).click()
 await page.getByRole('button',{name:'Сохранить вкус и продолжить'}).waitFor()
 await page.screenshot({path:`${out}/onboarding-desktop.png`,fullPage:true})
 await page.getByRole('button',{name:'Сохранить вкус и продолжить'}).click()
 await page.getByRole('button',{name:'Подобрать без ИИ'}).click()
 await page.locator('.release-card').first().waitFor();await page.waitForTimeout(300)
 await page.locator('.conversation').evaluate(el => el.scrollTo(0, 0)); await page.screenshot({path:`${out}/chat-desktop.png`}); if(phase==='after'){await page.getByRole('button',{name:'Настройки подборки',exact:true}).click(); await page.screenshot({path:`${out}/settings-desktop.png`}); await page.keyboard.press('Escape')}
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:`${out}/chat-mobile.png`}); if(phase==='after'){await page.getByRole('button',{name:'Настройки подборки',exact:true}).click();await page.screenshot({path:`${out}/settings-mobile.png`});await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:1000});await page.getByRole('button',{name:'Послушать позже',exact:true}).first().click();await page.getByRole('button',{name:'Моя коллекция',exact:true}).click();await page.screenshot({path:`${out}/collection-desktop.png`})}
 console.log(out)
}finally{await browser?.close();server.kill('SIGTERM')}
