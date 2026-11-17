import asyncio
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from PIL import Image,ImageDraw
from telethon import types

from app.ai import Model
from app.store import Store
from app.telegram import Telegram
from app.vision import save_visual


def animation_gif():
    frames=[Image.new('RGB',(96,96),color) for color in ('red','green','blue','yellow')]
    output=io.BytesIO();frames[0].save(output,'GIF',save_all=True,append_images=frames[1:],duration=200,loop=0)
    return output.getvalue()


def animation_tgs():
    vector={'v':'5.5.2','fr':30,'ip':0,'op':30,'w':128,'h':128,'assets':[],
        'layers':[{'ty':4,'ind':1,'ip':0,'op':30,'st':0,'bm':0,
        'ks':{'o':{'a':0,'k':100},'r':{'a':0,'k':0},'s':{'a':0,'k':[100,100,100]},'a':{'a':0,'k':[0,0,0]},
            'p':{'a':1,'k':[{'t':0,'s':[20,64,0],'e':[108,64,0],'o':{'x':.333,'y':.333},'i':{'x':.667,'y':.667}},
                            {'t':29,'s':[108,64,0]}]}},
        'shapes':[{'ty':'el','p':{'a':0,'k':[0,0]},'s':{'a':0,'k':[30,30]}},
                  {'ty':'fl','c':{'a':0,'k':[1,0,0,1]},'o':{'a':0,'k':100},'r':1}]}]}
    return gzip.compress(json.dumps(vector).encode())


def centers(image):
    return [image.getpixel((268+(i%2)*524,292+(i//2)*552)) for i in range(4)]


class VisualMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.store.update_settings({'enabled':True,'natural_enabled':False})

    async def asyncTearDown(self):
        self.store.close();self.tmp.cleanup()

    def event(self,raw,mime,attributes,text=''):
        document=SimpleNamespace(mime_type=mime,attributes=attributes)
        event=SimpleNamespace(raw_text=text,is_private=True,is_group=False,is_reply=False,mentioned=False,
            chat_id=10,id=101,message=SimpleNamespace(photo=False,document=document,file=SimpleNamespace(size=len(raw))),
            get_sender=AsyncMock(return_value=types.User(id=10,first_name='Оля')))
        receive=Mock();telegram=Telegram(self.store,receive)
        telegram.client=SimpleNamespace(download_media=AsyncMock(return_value=raw))
        return telegram,event,receive

    async def test_gif_is_downloaded_as_four_distinct_frames_and_reaches_model_context(self):
        raw=animation_gif();telegram,event,receive=self.event(raw,'image/gif',[types.DocumentAttributeAnimated()],'Смотри')
        await telegram.on_message(event)
        metadata=receive.call_args.kwargs
        self.assertEqual(metadata['image_kind'],'gif')
        path=self.root/metadata['image_path']
        with Image.open(path) as image:
            pixels=centers(image)
            self.assertGreater(pixels[0][0],200)
            self.assertGreater(pixels[2][2],200)
            self.assertGreater(pixels[3][1],200)
        self.store.message(10,'user','Смотри',101,**metadata)
        model=Model(self.store);model.completion=AsyncMock(return_value={'content':'Кадры меняют цвет'})
        try:
            await model.prepare_images(10)
            caption=model.completion.call_args.args[0][-1]['content'][0]['text']
            self.assertIn('Это GIF',caption)
            self.assertIn('от 1 до 4',caption)
            context=json.dumps(model.context(10),ensure_ascii=False)
            self.assertIn('[GIF: Кадры меняют цвет]',context)
            self.assertNotIn('base64',context)
        finally:await model.close()

    async def test_static_sticker_keeps_transparency_readable_and_uses_sticker_label(self):
        sticker=Image.new('RGBA',(96,96));ImageDraw.Draw(sticker).ellipse((20,20,76,76),fill='red')
        output=io.BytesIO();sticker.save(output,'WEBP',lossless=True)
        raw=output.getvalue();attribute=types.DocumentAttributeSticker(alt='🙂',stickerset=types.InputStickerSetEmpty())
        telegram,event,receive=self.event(raw,'image/webp',[attribute])
        await telegram.on_message(event)
        metadata=receive.call_args.kwargs
        self.assertEqual(metadata['image_kind'],'sticker')
        with Image.open(self.root/metadata['image_path']) as image:
            self.assertTrue(all(channel>240 for channel in image.getpixel((0,0))))
            self.assertGreater(image.getpixel((48,48))[0],220)
        self.assertIn('стикер',receive.call_args.args[1])

    async def test_tgs_sticker_renders_actual_movement_instead_of_its_emoji_only(self):
        attribute=types.DocumentAttributeSticker(alt='🙂',stickerset=types.InputStickerSetEmpty())
        telegram,event,receive=self.event(animation_tgs(),'application/x-tgsticker',[attribute])
        await telegram.on_message(event)
        metadata=receive.call_args.kwargs
        self.assertEqual(metadata['image_kind'],'sticker')
        with Image.open(self.root/metadata['image_path']) as image:
            frame_centers=[]
            for index in (0,3):
                x,y=12+(index%2)*524,36+(index//2)*552
                frame=image.crop((x,y,x+512,y+512))
                red=[px for px in range(512) if frame.getpixel((px,256))[0]>200 and frame.getpixel((px,256))[1]<80]
                self.assertTrue(red)
                frame_centers.append(sum(red)/len(red))
            self.assertGreater(frame_centers[1]-frame_centers[0],250)

    async def test_mp4_telegram_gif_and_webm_video_sticker_are_classified_and_decoded(self):
        self.assertIsNotNone(shutil.which('ffmpeg'),'FFmpeg is required for supported animation formats')
        for index,color in enumerate(('red','green','blue','yellow')):
            Image.new('RGB',(96,96),color).save(self.root/(str(index)+'.png'))
        for extension,codec,mime,attributes,kind in (
            ('mp4','libx264','video/mp4',[types.DocumentAttributeAnimated()],'gif'),
            ('webm','libvpx-vp9','video/webm',[types.DocumentAttributeSticker(alt='🙂',stickerset=types.InputStickerSetEmpty())],'sticker')):
            with self.subTest(mime=mime):
                output=self.root/('animation.'+extension)
                subprocess.run(['ffmpeg','-v','error','-nostdin','-framerate','4','-i',str(self.root/'%d.png'),
                    '-c:v',codec,'-threads','1','-pix_fmt','yuv420p',str(output)],check=True,capture_output=True,timeout=10)
                telegram,event,receive=self.event(output.read_bytes(),mime,attributes)
                await telegram.on_message(event)
                self.assertEqual(receive.call_args.kwargs['image_kind'],kind)
                with Image.open(self.root/receive.call_args.kwargs['image_path']) as image:
                    pixels=centers(image)
                    for pixel,expected in zip(pixels,[(255,0,0),(0,128,0),(0,0,255),(255,255,0)]):
                        self.assertTrue(all(abs(a-b)<35 for a,b in zip(pixel,expected)),(pixel,expected))

    async def test_regular_video_still_does_not_get_downloaded_as_a_gif(self):
        telegram,event,receive=self.event(b'ordinary video','video/mp4',[types.DocumentAttributeVideo(duration=2,w=100,h=100)])
        await telegram.on_message(event)
        telegram.client.download_media.assert_not_awaited()
        self.assertEqual(receive.call_args.kwargs['image_path'],'')

    async def test_invalid_tgs_and_external_assets_are_rejected(self):
        for raw in (b'not tgs',gzip.compress(b'{}'),gzip.compress(json.dumps({'w':128,'h':128,'assets':[{'p':'secret.png','u':'/etc/'}]}).encode())):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):save_visual(self.root,raw,'sticker','application/x-tgsticker')
