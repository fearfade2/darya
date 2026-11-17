"""Download fixed, free local voice weights; never runs in response to chat input."""
import hashlib
import json
from pathlib import Path
import httpx

root = Path(__file__).resolve().parents[1] / 'data' / 'voice-models'
root.mkdir(parents=True, exist_ok=True, mode=0o700)
manifest = {}
with httpx.Client(follow_redirects=True, timeout=120) as client:
    for repo, names, directory in [
        ('Systran/faster-whisper-base', ['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt', 'README.md'], 'whisper-base')]:
        metadata = client.get(f'https://huggingface.co/api/models/{repo}')
        metadata.raise_for_status()
        revision = metadata.json()['sha']
        target = root / directory
        target.mkdir(exist_ok=True, mode=0o700)
        for name in names:
            path = target / Path(name).name
            url = f'https://huggingface.co/{repo}/resolve/{revision}/{name}'
            if not path.exists():
                partial = path.with_suffix(path.suffix + '.part')
                with client.stream('GET', url) as response:
                    response.raise_for_status()
                    with partial.open('wb') as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
                partial.replace(path)
                path.chmod(0o600)
            manifest[str(path.relative_to(root))] = {'repository': repo, 'revision': revision,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
            print(directory, path.name, path.stat().st_size, flush=True)
    url = 'https://models.silero.ai/models/tts/ru/v5_6_ru.pt'
    expected = '04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e'
    path = root / 'silero/v5_6_ru.pt'
    path.parent.mkdir(exist_ok=True,mode=0o700)
    if not path.exists():
        partial = path.with_suffix('.part')
        with client.stream('GET',url) as response:
            response.raise_for_status()
            with partial.open('wb') as output:
                for chunk in response.iter_bytes(): output.write(chunk)
        if hashlib.sha256(partial.read_bytes()).hexdigest() != expected:
            partial.unlink(missing_ok=True)
            raise ValueError('Silero model checksum differs from the verified release')
        partial.replace(path);path.chmod(0o600)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('Silero model checksum mismatch')
    manifest['silero/v5_6_ru.pt'] = {'source':url,'sha256':expected,'bytes':path.stat().st_size,'default_speaker':'xenia'}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2))
