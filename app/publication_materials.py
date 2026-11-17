"""Separate linked news in a digest without separating tutorials or albums."""
import hashlib
import re

from .research import public_link


def digest_parts(text, references):
    entries=list(re.finditer(r'(?m)^[ \t]*(\d{1,2})[.)][ \t]+',text))
    if (not 3<=len(entries)<=40 or [int(entry[1]) for entry in entries]!=list(range(1,len(entries)+1))
            or not re.search(r'подборк|находк|дайджест|новост|материал|ссылк',text[:entries[0].start()],re.I)):
        return []
    parts=[]
    for index,entry in enumerate(entries):
        end=entries[index+1].start() if index+1<len(entries) else len(text)
        block=text[entry.end():end].strip()
        block=re.split(r'\n\n(?:🤖\s*Пост собран|Пост собран автоматически)',block,maxsplit=1)[0].strip()
        if not block or len(block)>1200:return []
        values=[ref['url'] for ref in references if entry.start()<=ref['start'] and ref['end']<=end]
        values+=re.findall(r'https?://[^\s<>"«»]+',block)
        links=[]
        for value in values:
            try:link=public_link(value.rstrip('.,;!…'))
            except ValueError:continue
            if link not in links:links.append(link)
        key='\n'.join(sorted(links)) if links else re.sub(r'\s+',' ',block)
        parts.append({'title':block.split('\n',1)[0][:250],'text':block,'links':links,
                      'part_id':hashlib.sha256(key.encode()).hexdigest()})
    return parts if sum(bool(part['links']) for part in parts)>=2 else []
