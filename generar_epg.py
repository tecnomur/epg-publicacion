import os, re, json, gzip, copy, unicodedata, urllib.request, hashlib
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

def download(url):
    if not url.startswith(("https://", "http://")):
        raise ValueError("Esquema de URL inválido")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"TecnoMur-EPG/1.0"}), timeout=90) as r:
        data=r.read(100_000_001)
    if len(data)>100_000_000: raise ValueError("Archivo demasiado grande")
    return gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data

def norm(s):
    s=unicodedata.normalize("NFKD",s.casefold())
    s="".join(c for c in s if not unicodedata.combining(c))
    s=re.sub(r"^(?:es\s*[:|_-]\s*|espana\s*[:|_-]\s*)","",s)
    s=re.sub(r"(?:\s*full\s*hd|\s*(?:uhd|fhd|hd|sd|4k|hevc|1080p|720p))\+?(?=\s|$)"," ",s)
    s=re.sub(r"\blcampeones\b","liga de campeones",s)
    s=re.sub(r"^m(?:\+|\s)","m+ ",s)
    s=re.sub(r"^movistar\s+","m+ ",s)
    return re.sub(r"[^a-z0-9]+","",s)

def alternate(s):
    s=unicodedata.normalize("NFKD",s.casefold())
    s="".join(c for c in s if not unicodedata.combining(c))
    s=re.sub(r"\s+solo eventos\s*$","",s)
    s=re.sub(r"\s+235\s*$","",s)
    s=re.sub(r"(?<=\w)(?:full\s*hd|fhd|uhd|hd|sd|4k)\+?\s*$","",s)
    s=re.sub(r"^m\+\s*lcampeon(?:es)?\s*(\d*)\s*$",r"M+ Liga de Campeones \1",s)
    s=re.sub(r"^m\+\s*laligatv\s*(\d*)\s*$",r"M+ LaLiga \1",s)
    s=re.sub(r"^m\+\s*deportes\s*(\d+)\s*$",r"M+ Deportes \1",s)
    s=re.sub(r"^dazn\s+([1-4])\s+esp$",r"DAZN \1",s)
    return norm(s)

def spelling_key(s):
    # Some provider feeds replace accented letters with two spaces.
    s=unicodedata.normalize("NFC",s.casefold())
    s=re.sub(r"[áéíóúüñçàèìòùâêîôû]", "  ", s)
    return norm(s)

def fallback_keys(s):
    s=re.sub(r"^\s*(?:\[(?:es|esp)\]|\|(?:es|esp)\||(?:esp|spain)\s*[:|_-])\s*","",s,flags=re.I)
    s=re.sub(r"\s+(?:full|hd\+|fhd\+|1080i|2160p)\s*$","",s,flags=re.I)
    return {norm(s),alternate(s),spelling_key(s)}-{""}

def playlist(data):
    for line in data.decode("utf-8-sig",errors="replace").splitlines():
        if not line.startswith("#EXTINF:"): continue
        parts=re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)',line,maxsplit=1)
        if len(parts)!=2: continue
        attrs=dict(re.findall(r'([\w-]+)="([^"]*)"',parts[0]))
        name=parts[1].strip()
        yield attrs.get("tvg-id","").strip(), attrs.get("tvg-name","").strip(),name

def main():
    cfg=json.loads(Path("config.json").read_text())
    manual=cfg["aliases"]
    sources=[]
    for source in cfg["sources"]:
        try:
            root=ET.fromstring(download(source["url"]))
            programmes=defaultdict(list)
            for p in root.findall("programme"):
                programmes[p.get("channel")].append(p)
            channels={c.get("id"):c for c in root.findall("channel") if programmes[c.get("id")]}
            index=defaultdict(set)
            for cid,c in channels.items():
                for n in [cid,*[x.text or "" for x in c.findall("display-name")]]:
                    if norm(n): index[norm(n)].add(cid)
                    k=spelling_key(n)
                    if k and k!=norm(n): index["spell:"+k].add(cid)
            sources.append((source["name"],channels,programmes,index))
        except Exception:
            print("Fuente no disponible:",source["name"])
    if not sources: raise SystemExit("Sin fuentes EPG disponibles")
    url=os.environ.get("M3U_URL","").strip()
    if not url: raise SystemExit("Falta el secreto M3U_URL")
    if not url.startswith(("https://","http://")): raise SystemExit("M3U_URL debe comenzar por http:// o https://")
    if url.startswith("http://"): print("Aviso: proveedor HTTP sin cifrado")
    try: rows=list(playlist(download(url)))
    except urllib.error.HTTPError as e: raise SystemExit(f"Descarga IPTV: HTTP {e.code}") from None
    except urllib.error.URLError as e: raise SystemExit(f"Descarga IPTV: error de conexión ({type(e.reason).__name__})") from None
    except Exception as e: raise SystemExit(f"Descarga IPTV: {type(e).__name__}") from None
    if not rows: raise SystemExit("Lista vacía")
    result=ET.Element("tv",{"generator-info-name":"TecnoMur EPG Manager"})
    out=[]; missing=[]; matched=[]; groups=defaultdict(list)
    for cid,tvg,name in rows:
        if cid.casefold() in ("ext","unknown","none","null","0"):
            key="tm-"+hashlib.sha256((tvg or name).encode()).hexdigest()[:24]
            groups[key].append((tvg,name))
        elif cid: groups[cid].append((tvg,name))
        # Entries with no ID are not processed by default (often VOD).
    for cid,names in groups.items():
        # Reject URL-like or potentially credential-bearing identifiers.
        if not re.fullmatch(r"[\w .+&()#:-]{1,160}",cid) or "://" in cid:
            missing.append({"reason":"identificador no seguro"}); continue
        found=set()
        for tvg,name in names:
            alias=manual.get(name,manual.get(tvg,manual.get(cid)))
            for idx,(label,channels,programmes,index) in enumerate(sources):
                if alias:
                    candidates={alias} if alias in channels else index.get(norm(alias),set())
                elif cid in channels: candidates={cid}
                else:
                    candidates=set()
                    for n in (tvg,name):
                        if n: candidates.update(index.get(norm(n),set()))
                if len(candidates)==1:
                    found.add((idx,next(iter(candidates)))); break
        if not found:
            for tvg,name in names:
                for idx,(label,channels,programmes,index) in enumerate(sources):
                    candidates=set()
                    for n in (tvg,name):
                        if n and alternate(n)!=norm(n):
                            candidates.update(index.get(alternate(n),set()))
                    if len(candidates)==1:
                        found.add((idx,next(iter(candidates)))); break
        if not found:
            for tvg,name in names:
                for idx,(label,channels,programmes,index) in enumerate(sources):
                    candidates=set()
                    for n in (tvg,name):
                        if not n: continue
                        for k in fallback_keys(n):
                            candidates.update(index.get(k,set()))
                        if re.search(r"\w\s{2,}\w",n):
                            candidates.update(index.get("spell:"+spelling_key(n),set()))
                    if len(candidates)==1:
                        found.add((idx,next(iter(candidates)))); break
        if len(found)!=1:
            missing.append({"id":cid,"names":names,"reason":"ambiguo o sin coincidencia"}); continue
        idx,original=next(iter(found))
        label,channels,programmes,index=sources[idx]
        ch=ET.SubElement(result,"channel",{"id":cid})
        for display in dict.fromkeys([n for pair in names for n in pair if n]):
            if re.fullmatch(r"[\w .+&()#:\-]{1,160}",display) and "://" not in display:
                ET.SubElement(ch,"display-name").text=display
        ET.SubElement(ch,"display-name").text=channels[original].findtext("display-name") or original
        for p in programmes[original]:
            q=copy.deepcopy(p); q.set("channel",cid); out.append(q)
        matched.append({"id":cid,"source":label,"original":original})
    Path("output").mkdir(exist_ok=True)
    print("Canales totales:",len(rows),"IDs únicos:",len(groups),"IDs asociados:",len(matched))
    if not matched or len(matched)/max(1,len(groups))<cfg["minimum_coverage"]:
        raise SystemExit("Cobertura insuficiente; no se sustituye la guía")
    previous=Path("epg.xml.gz")
    if previous.exists():
        old_ids={c.get("id") for c in ET.fromstring(gzip.decompress(previous.read_bytes())).findall("channel")}
        new_ids={c.get("id") for c in result.findall("channel")}
        if old_ids-new_ids or len(new_ids)<len(old_ids):
            raise SystemExit("Protección: se perderían demasiadas asociaciones existentes")
    result.extend(out)
    Path("output/epg.xml.gz").write_bytes(gzip.compress(ET.tostring(result,encoding="utf-8",xml_declaration=True),mtime=0))
    print("IDs asociados:",len(matched),"pendientes:",len(missing))
if __name__=="__main__": main()
