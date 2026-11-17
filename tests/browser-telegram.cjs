const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const base=process.argv[2]||'http://127.0.0.1:8792';
  const directory=process.argv[3]||'/tmp/darya-autonomy-i13_uhsd/validation';
  const password=fs.readFileSync(path.join(directory,'ui-data/first-login.txt'),'utf8').split('\n')[1];
  const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:process.env.INSTABOX_HEADLESS==='1',args:['--no-sandbox','--disable-quic']});
  const context=await browser.newContext({viewport:{width:1440,height:1080}});
  const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  try{
    await page.goto(base);
    await page.locator('#login-screen').waitFor({state:'visible'});
    await page.locator('#login-password').fill(password);await page.locator('#login-form button').click();
    await page.locator('#app-shell').waitFor({state:'visible'});
    await page.route('**/api/telegram/media?*',route=>route.fulfill({json:{items:[{asset_id:'sticker:123',kind:'sticker',label:'😄',pack:'AnimePack'}]}}));
    await page.route('**/api/telegram/media/*/preview',route=>route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" rx="20" fill="#f0e6da"/><text x="80" y="110" text-anchor="middle" font-size="96">😄</text></svg>'}));
    await page.locator('[data-page="telegram"]').click();
    await page.locator('#tg-autonomy-state').filter({hasText:'Самостоятельность'}).waitFor();
    assert.equal(await page.locator('#tg-autonomy-permissions input').count(),10);
    await page.locator('#tg-autonomy-enabled').check();
    await page.locator('#tg-autonomy-minutes').fill('15');
    await page.locator('#tg-private-enabled').check();
    await page.locator('#tg-private-min').fill('20');
    await page.locator('#tg-private-max').fill('120');
    await page.locator('#tg-private-unanswered').fill('2');
    await page.locator('#tg-private-daily').fill('5');
    await page.locator('#tg-autonomy-instructions').fill('Ищи забавные стикеры и GIF');
    await page.locator('#tg-autonomy-form button[type="submit"]').click();
    await page.waitForFunction(()=>document.getElementById('toast').textContent==='Самостоятельность сохранена');
    const settings=await page.evaluate(async()=>await(await fetch('/api/status')).json());
    assert.equal(settings.settings.telegram_autonomy_enabled,true);assert.equal(settings.settings.telegram_autonomy_minutes,15);
    assert.equal(settings.settings.private_initiative_enabled,true);assert.equal(settings.settings.private_initiative_max_unanswered,2);
    await page.locator('#tg-stickers-enabled').check();await page.locator('#tg-gifs-enabled').check();
    await page.locator('#tg-media-settings button').click();
    await page.waitForFunction(()=>document.getElementById('toast').textContent.includes('Поведение со стикерами'));
    const captured=[];let runId=900;
    await page.route('**/api/telegram/actions',async route=>{
      const action=route.request().postDataJSON();captured.push(action);runId++;
      await route.fulfill({json:{accepted:true,run_id:runId}});
    });
    await page.route('**/api/telegram/actions/*',route=>{
      const action=captured[captured.length-1];
      const result=action?.action==='call'?{connected:true,name:'Оля',mode:'talk'}:{sent:true,text:'Отправлено'};
      return route.fulfill({json:{run:{id:runId,status:'complete'},actions:[{action:action?.action,status:'sent',result:JSON.stringify(result)}]}});
    });
    await page.locator('#tg-action-kind').selectOption('call');
    assert.equal(await page.locator('#tg-action-text-field').isVisible(),false);
    await page.locator('#tg-recipient').fill('@olga_test');await page.locator('#tg-action-form button').click();
    await page.locator('#tg-action-result').filter({hasText:'В звонке: Оля'}).waitFor();
    assert.deepEqual(captured[0],{action:'call',recipient:'@olga_test'});
    await page.locator('#tg-action-kind').selectOption('poll');
    assert.equal(await page.locator('#tg-poll-field').isVisible(),true);
    await page.locator('#tg-action-text').fill('Джаз или рок?');await page.locator('#tg-poll-options').fill('Джаз\nРок');
    await page.locator('#tg-action-form button').click();
    await page.locator('#tg-action-result').filter({hasText:'Отправлено'}).waitFor();
    assert.deepEqual(captured[1].options,['Джаз','Рок']);
    await page.locator('#tg-action-kind').selectOption('publish_story');
    assert.equal(await page.locator('#tg-file-field').isVisible(),true);
    assert.equal(await page.locator('#tg-recipient-field').isVisible(),false);
    await page.route('**/api/telegram/media/search',async route=>{
      assert.deepEqual(route.request().postDataJSON(),{kind:'gif',query:'котики'});
      await route.fulfill({json:{items:[{asset_id:'gif-inline:1:cat',kind:'gif',label:'Кот смеётся',pack:''}]}});
    });
    await page.locator('#tg-media-kind').selectOption('gif');
    await page.locator('#tg-media-query').fill('котики');await page.locator('#tg-media-search button[type="submit"]').click();
    await page.locator('#tg-media-gallery').filter({hasText:'Кот смеётся'}).waitFor();
    await page.locator('#tg-media-recipient').fill('@olga_test');
    await page.locator('#tg-media-gallery button').filter({hasText:'Отправить'}).click();
    await page.locator('#tg-media-status').filter({hasText:'Отправлено'}).waitFor();
    assert.deepEqual(captured[2],{action:'send_gif',recipient:'@olga_test',asset_id:'gif-inline:1:cat'});
    await page.screenshot({path:path.join(directory,'telegram-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    for(const selector of ['#tg-autonomy-form','#tg-action-form','#tg-media-gallery','#tg-action-journal']){
      await page.locator(selector).scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'Mobile page must not overflow');
    }
    await page.locator('#tg-media-gallery').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(directory,'telegram-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,actionsChecked:captured.length,desktop:'telegram-desktop.png',mobile:'telegram-mobile.png'}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
