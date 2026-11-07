import { chromium } from '../frontend/node_modules/playwright/index.mjs'
import AxeBuilder from '../frontend/node_modules/@axe-core/playwright/dist/index.mjs'
import fs from 'node:fs/promises'
// Isolated demo server only: scripts/web_test_server.py on port 8767.
const root=process.env.DISCOVERY_AUDIT_OUTPUT || new URL('../artifacts/evals/ecc-unslop-20261106', import.meta.url).pathname
await fs.mkdir(root,{recursive:true})
const phase=process.argv[2] || 'after'
if (!['before','after'].includes(phase)) throw new Error('Phase must be before or after')
const browser=await chromium.launch({headless:true})
const context=await browser.newContext({viewport:{width:1440,height:1000}})
const page=await context.newPage()
const errors=[], failures=[], checks=[]
await page.addInitScript(() => {
 window.__auditVitals = {lcp: null, cls: 0, longestInteraction: null}
 new PerformanceObserver(list => { for(const e of list.getEntries()) window.__auditVitals.lcp=e.startTime }).observe({type:'largest-contentful-paint',buffered:true})
 new PerformanceObserver(list => { for(const e of list.getEntries()) if(!e.hadRecentInput) window.__auditVitals.cls+=e.value }).observe({type:'layout-shift',buffered:true})
 new PerformanceObserver(list => { for(const e of list.getEntries()) if(e.interactionId) window.__auditVitals.longestInteraction=Math.max(window.__auditVitals.longestInteraction||0,e.duration) }).observe({type:'event',buffered:true,durationThreshold:16})
})
page.on('pageerror', e=>errors.push(e.message))
page.on('console', m=>{if(m.type()==='error') errors.push(m.text())})
page.on('response', r=>{if(r.status()>=400 && !r.url().endsWith('/auth/session')) failures.push({path:new URL(r.url()).pathname,status:r.status()})})
async function capture(name,width=1440){
 await page.setViewportSize({width,height:1000})
 await page.screenshot({path:`${root}/${phase}-${name}-${width}.png`,fullPage:true})
 const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa','best-practice']).analyze()
 const geometry=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth, mainOverflow:document.querySelector('main')?.scrollWidth>innerWidth, firstCardY:document.querySelector('.release-card')?.getBoundingClientRect().y, landmarks:document.querySelectorAll('main').length,vitals:window.__auditVitals}))
 checks.push({name,width,geometry,violations:axe.violations.map(v=>({id:v.id,impact:v.impact,description:v.description,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))})
}
try {
await page.goto('http://127.0.0.1:8767')
await page.getByRole('button',{name:'Войти',exact:true}).waitFor()
await capture('login')
await page.getByRole('button',{name:'Нет аккаунта? Зарегистрироваться'}).click()
await page.getByLabel('Логин',{exact:true}).fill('audit_'+Date.now())
await page.getByLabel('Пароль',{exact:true}).fill('audit-test-password-only')
await page.getByLabel('Подтвердите пароль').fill('audit-test-password-only')
await page.getByRole('button',{name:'Создать аккаунт',exact:true}).click()
await page.getByRole('heading',{name:'Что вам нравится?'}).waitFor()
await capture('onboarding')
await page.getByRole('button',{name:'Сохранить вкус и продолжить'}).click()
await page.getByRole('heading',{name:'С чего начнём слушать?'}).waitFor()
await capture('empty-chat')
await page.getByLabel('Сообщение помощнику').fill('Подбери релизы')
await page.getByRole('button',{name:'Отправить сообщение'}).click()
await page.locator('.release-card').first().waitFor()
await page.locator('.release-card').first().getByRole('button',{name:'Прослушал — нравится',exact:true}).click()
await page.getByLabel('Сообщение помощнику').waitFor({state:'visible'})
for(const width of [1440,768,375]) await capture('chat',width)
await page.setViewportSize({width:1440,height:1000})
await page.getByRole('button',{name:'Настройки подборки'}).click()
await capture('settings')
await page.getByRole('button',{name:'Отмена',exact:true}).click()
await page.getByRole('button',{name:'Моя коллекция'}).click()
await page.locator('.collection-entry').first().waitFor()
for(const width of [1440,768,375]) await capture('collection',width)
await page.setViewportSize({width:1440,height:1000})
await page.getByRole('button',{name:'Очистить коллекцию'}).click()
await capture('confirmation')
await page.getByRole('button',{name:'Отмена',exact:true}).click()
await page.getByRole('button',{name:'Настройки вкуса'}).click()
await capture('profile')
await fs.writeFile(`${root}/${phase}-ui.json`,JSON.stringify({phase,checks,errors,failures},null,2))
console.log(JSON.stringify({phase,checks:checks.map(c=>({name:c.name,width:c.width,violations:c.violations.map(v=>v.id),...c.geometry})),errors,failures},null,2))
} finally { await browser.close() }
