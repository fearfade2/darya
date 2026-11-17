from __future__ import annotations

import base64
import gzip
import io
import json
import math
import secrets
import subprocess
import tempfile
import time
import warnings
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

MAX_BYTES = 12 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 24_000_000


def normalize(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("Фото слишком большое: отправьте изображение до 12 МБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((1600, 1600))
                image = white_background(image)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=86)
                return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Не удалось прочитать изображение. Отправьте фото в JPEG, PNG или WebP") from None


def white_background(image):
    rgba = image.convert('RGBA')
    background = Image.new('RGBA',rgba.size,'white')
    background.alpha_composite(rgba)
    return background.convert('RGB')


def frame_indices(count):
    return sorted({round((count-1)*i/3) for i in range(4)})


def frame_sheet(frames):
    if not frames:
        raise ValueError('Не удалось прочитать кадры анимации')
    sheet = Image.new('RGB',(1048,1104),'#eeeeee')
    draw = ImageDraw.Draw(sheet)
    for index,frame in enumerate(frames[:4]):
        frame = white_background(frame)
        frame.thumbnail((512,512))
        x,y = 12+(index%2)*524,12+(index//2)*552
        draw.text((x+4,y+4),str(index+1),fill='black')
        sheet.paste(frame,(x+(512-frame.width)//2,y+24+(512-frame.height)//2))
    output=io.BytesIO();sheet.save(output,'JPEG',quality=90)
    return output.getvalue()


def pillow_frames(raw):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                count=getattr(source,'n_frames',1)
                if count>2000:
                    raise ValueError('В анимации слишком много кадров')
                frames=[]
                for index in frame_indices(count):
                    source.seek(index)
                    frame=source.convert('RGBA');frame.thumbnail((512,512));frames.append(frame)
                return frames
    except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError('Не удалось прочитать изображение или GIF') from None


def tgs_frames(raw):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
            data=source.read(2*1024*1024+1)
        if len(data)>2*1024*1024:
            raise ValueError()
        animation=json.loads(data)
        if not isinstance(animation,dict) or not 1<=animation.get('w',0)<=1024 or not 1<=animation.get('h',0)<=1024:
            raise ValueError()
        if any(asset.get('p') or asset.get('u') for asset in animation.get('assets',[])):
            raise ValueError()
        from rlottie_python import LottieAnimation
        renderer=LottieAnimation.from_data(data.decode('utf-8'))
        try:
            count=renderer.lottie_animation_get_totalframe()
            if not 1<=count<=2000:
                raise ValueError()
            return [renderer.render_pillow_frame(index,width=512,height=512).copy() for index in frame_indices(count)]
        finally:
            renderer.lottie_animation_destroy()
    except (OSError,EOFError,ValueError,TypeError,AttributeError,ImportError,RuntimeError):
        raise ValueError('Не удалось прочитать анимированный стикер') from None


def video_frames(raw,mime):
    try:
        deadline=time.monotonic()+15
        with tempfile.TemporaryDirectory(prefix='darya-frames-') as directory:
            folder=Path(directory);source=folder/('animation.webm' if mime=='video/webm' else 'animation.mp4')
            source.write_bytes(raw);source.chmod(0o600)
            probe=subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-select_streams','v:0',
                '-show_entries','stream=width,height,duration,codec_name,avg_frame_rate,r_frame_rate:format=duration','-of','json',str(source)],
                check=True,capture_output=True,timeout=5)
            info=json.loads(probe.stdout);stream=info['streams'][0]
            if not 0<stream['width']*stream['height']<=24_000_000:
                raise ValueError()
            duration=float(info.get('format',{}).get('duration') or stream.get('duration') or 2)
            if not math.isfinite(duration) or duration<=0:
                raise ValueError()
            rate=30
            for name in ('avg_frame_rate','r_frame_rate'):
                try:
                    candidate=float(Fraction(stream.get(name) or '0/0'))
                    if math.isfinite(candidate) and candidate>0:
                        rate=candidate
                        break
                except (ValueError,ZeroDivisionError):
                    pass
            # Seeking beyond the last frame yields no image, especially in short, slow clips.
            last_time=max(0,min(duration,300)-1/rate)
            frames=[]
            for index in range(4):
                output=folder/(str(index)+'.png')
                decoder=['-c:v','libvpx-vp9'] if mime=='video/webm' and stream.get('codec_name')=='vp9' else []
                subprocess.run(['ffmpeg','-v','error','-nostdin','-threads','1','-protocol_whitelist','file,pipe',
                    '-ss',str(last_time*index/3),*decoder,'-i',str(source),'-frames:v','1',
                    '-vf','scale=512:512:force_original_aspect_ratio=decrease','-threads','1',str(output)],
                    check=True,capture_output=True,timeout=max(.1,min(8,deadline-time.monotonic())))
                if output.is_file():
                    with Image.open(output) as frame:frames.append(frame.copy())
            return frames
    except (OSError,ValueError,KeyError,IndexError,subprocess.SubprocessError):
        raise ValueError('Не удалось прочитать кадры GIF или видеостикера') from None


def save_visual(directory,raw,kind='photo',mime='image/jpeg'):
    if not isinstance(raw,bytes) or not raw or len(raw)>MAX_BYTES:
        raise ValueError('Вложение должно быть до 12 МБ')
    if kind=='photo':
        return save_image(directory,raw)
    if mime=='application/x-tgsticker':
        frames=tgs_frames(raw)
    elif mime in ('video/mp4','video/webm'):
        frames=video_frames(raw,mime)
    else:
        frames=pillow_frames(raw)
    if len(frames)==1:
        output=io.BytesIO();white_background(frames[0]).save(output,'JPEG',quality=90)
        return save_image(directory,output.getvalue())
    return save_image(directory,frame_sheet(frames))


def save_image(directory, raw):
    data = normalize(raw)
    folder = directory / "incoming"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (secrets.token_hex(16) + ".jpg")
    path.write_bytes(data)
    path.chmod(0o600)
    return str(path.relative_to(directory))


def image_url(directory, relative):
    original = directory / relative
    path = original.resolve()
    if path.parent != (directory / "incoming").resolve() or not path.is_file() or original.is_symlink():
        raise ValueError("Фото не найдено")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("Фото слишком большое")
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def sample_image():
    image = Image.new("RGB", (320, 240), "white")
    from PIL import ImageDraw
    ImageDraw.Draw(image).rectangle((60, 50, 260, 190), fill="red")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
