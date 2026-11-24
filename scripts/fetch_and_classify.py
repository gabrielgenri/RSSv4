import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from html import unescape
from urllib.parse import urljoin
import feedparser
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ARG_TZ = timezone(timedelta(hours=-3))
FEEDS_PATH = "data/feeds_argentina_larioja.json"
OUT_PATH = "data/noticias_consolidadas.json"
MAX_ITEMS_TOTAL = 600
MAX_VERSIONS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def extract_image(entry, base_url: str = "") -> str:
    for attr in ("media_content", "media_thumbnail"):
        media_list = getattr(entry, attr, None)
        if isinstance(media_list, list):
            for m in media_list:
                u = (m.get("url") or "").strip() if isinstance(m, dict) else ""
                if u:
                    return urljoin(base_url, u) if base_url else u

    for enc in getattr(entry, "enclosures", None) or []:
        mtype = (enc.get("type") or "").lower() if isinstance(enc, dict) else ""
        href = (enc.get("href") or "").strip() if isinstance(enc, dict) else ""
        if href and ("image" in mtype or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", href, re.I)):
            return urljoin(base_url, href) if base_url else href

    for field in ("content", "summary", "description"):
        val = getattr(entry, field, None)
        blobs = []
        if isinstance(val, list):
            for c in val:
                if isinstance(c, dict) and c.get("value"):
                    blobs.append(c["value"])
        elif isinstance(val, str):
            blobs.append(val)
        for html_blob in blobs:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_blob, re.I)
            if m:
                src = m.group(1).strip()
                if src and not src.startswith("data:"):
                    return urljoin(base_url, src) if base_url else src

    return ""


def extract_body_and_lead(entry, link: str = "") -> tuple[str, str]:
    lead_raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    copete = strip_html(lead_raw)[:300]

    body_parts = []
    content_list = getattr(entry, "content", None)
    if isinstance(content_list, list):
        for c in content_list:
            if isinstance(c, dict) and c.get("value"):
                body_parts.append(strip_html(c["value"]))
    if lead_raw:
        body_parts.append(strip_html(lead_raw))

    cuerpo = " ".join(part for part in body_parts if part).strip()
    if len(cuerpo) < 180 and link and link.startswith(("http://", "https://")):
        try:
            r = requests.get(link, headers=HEADERS, timeout=10, verify=False)
            if r.ok and "text/html" in r.headers.get("Content-Type", "").lower():
                page_html = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
                paras = [strip_html(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", page_html, flags=re.S | re.I)]
                good_paras = [p for p in paras if len(p) >= 45]
                if good_paras:
                    article_text = " ".join(good_paras[:8])[:1600]
                    if len(article_text) > len(cuerpo):
                        cuerpo = article_text
        except Exception:
            pass

    if not copete and cuerpo:
        copete = cuerpo[:260]
    if not cuerpo:
        cuerpo = copete or "Contenido sin cuerpo extendido en el canal RSS."

    return copete, cuerpo[:1600]


def classify_topic(text: str, rules: dict, default_topic: str) -> tuple[str, list[str]]:
    haystack = text.lower()
    best_topic = default_topic
    best_score = 0
    matched_tags = []

    for topic, keywords in rules.items():
        hits = [kw for kw in keywords if re.search(r"\b" + re.escape(kw.lower()) + r"\b", haystack)]
        if hits:
            matched_tags.append(topic.split(",")[0].strip())
            for h in hits[:2]:
                matched_tags.append(h.title())
        if len(hits) > best_score:
            best_score = len(hits)
            best_topic = topic

    dedup_tags = list(dict.fromkeys(matched_tags))[:6]
    return best_topic, dedup_tags


def parse_entry_date(entry, fallback_iso: str) -> str:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        dt_struct = getattr(entry, attr, None)
        if dt_struct:
            try:
                dt_utc = datetime(*dt_struct[:6], tzinfo=timezone.utc)
                return dt_utc.astimezone(ARG_TZ).isoformat()
            except Exception:
                pass
    return fallback_iso


def fetch_feed_xml(url: str, timeout: int = 15):
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
    resp.raise_for_status()
    return resp.content


def build_linked_syntheses(items: list[dict], run_iso: str) -> list[dict]:
    by_topic = defaultdict(list)
    for it in items:
        by_topic[it.get("tema", "Política y Gestión")].append(it)

    syntheses = []
    for tema, group in by_topic.items():
        unique_sources = []
        seen_src = set()
        for g in group:
            sname = g.get("fuente_nombre", "")
            if sname and sname not in seen_src:
                seen_src.add(sname)
                unique_sources.append(sname)

        if len(unique_sources) < 2 and len(group) < 2:
            continue

        top_notes = group[:4]
        ambitos = sorted({n.get("ambito", "Nacional") for n in top_notes})
        ambitos_txt = " y ".join(ambitos)
        fuentes_txt = ", ".join(unique_sources[:4])

        resumen_notas = " ".join(
            f"Desde {n.get('fuente_nombre')} ({n.get('ambito')}) se reporta: '{n.get('titulo')}'."
            for n in top_notes
        )
        cuerpo_original = (
            f"Síntesis transversal generada a partir de {len(top_notes)} notas vinculadas en la taxonomía "
            f"'{tema}' across {ambitos_txt}, reuniendo aportes de {fuentes_txt}. "
            f"{resumen_notas} "
            f"Al contrastar estas fuentes en un mismo eje temático, se identifican puntos de convergencia "
            f"y contrastes entre la agenda territorial de La Rioja y el contexto nacional para facilitar una lectura integral."
        )

        slug = hashlib.sha1(tema.encode("utf-8")).hexdigest()[:10]
        syntheses.append({
            "id": f"sintesis-{slug}",
            "tema": tema,
            "fecha_generacion": run_iso,
            "titulo_original": f"Síntesis cruzada · {tema} ({ambitos_txt})",
            "copete": f"Vincula {len(top_notes)} notas de {len(unique_sources)} canales bajo '{tema}'.",
            "cuerpo_original": cuerpo_original,
            "fuentes_vinculadas": unique_sources[:5],
            "notas_vinculadas": [
                {
                    "id": n.get("id"),
                    "titulo": n.get("titulo"),
                    "fuente_nombre": n.get("fuente_nombre"),
                    "ambito": n.get("ambito"),
                    "enlace": n.get("enlace"),
                }
                for n in top_notes
            ],
            "etiquetas": list(dict.fromkeys([tema.split(",")[0].strip()] + ambitos + ["Contenido Vinculado"]))[:6],
        })

    syntheses.sort(key=lambda s: len(s.get("notas_vinculadas", [])), reverse=True)
    return syntheses


def main():
    now_arg = datetime.now(ARG_TZ)
    run_iso = now_arg.isoformat()
    run_id = now_arg.strftime("%Y-%m-%d-%H%M")
    run_label = now_arg.strftime("%d/%m/%Y · %H:%M")

    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    rules = config.get("metadata", {}).get("reglas_clasificacion", {})
    fuentes = [src for src in config.get("fuentes", []) if src.get("activo", True)]

    existing = {"items": [], "versiones_disponibles": []}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        except Exception:
            pass

    existing_items = existing.get("items", [])
    seen_keys = set()
    for item in existing_items:
        key = item.get("enlace") or item.get("id")
        if key:
            seen_keys.add(key)

    new_items = []
    fuentes_ok = 0
    fuentes_error = 0

    for src in fuentes:
        url = src.get("url_rss")
        if not url:
            continue
        try:
            raw_xml = fetch_feed_xml(url)
            parsed = feedparser.parse(raw_xml)
            if parsed.bozo and not parsed.entries:
                raise RuntimeError(f"Parse error: {parsed.bozo_exception}")
            fuentes_ok += 1

            for entry in parsed.entries[:18]:
                title = strip_html(getattr(entry, "title", "") or "")
                link = (getattr(entry, "link", "") or "").strip()
                if not title and not link:
                    continue

                dedup_key = link if link else f"{src['id']}::{title}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                copete, cuerpo = extract_body_and_lead(entry, link)
                imagen = extract_image(entry, link or url)
                item_id = hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()[:12]

                tema, extra_tags = classify_topic(
                    f"{title} {copete} {cuerpo[:400]}",
                    rules,
                    src.get("tema_por_defecto", "Política y Gestión"),
                )
                tags = list(dict.fromkeys([src.get("ambito", "Nacional"), src.get("tipo_entidad", "Medio")] + extra_tags))[:6]

                new_items.append({
                    "id": item_id,
                    "titulo": title or "(Sin título)",
                    "copete": copete or "Sin copete disponible.",
                    "cuerpo": cuerpo,
                    "resumen": copete or cuerpo[:280],
                    "imagen": imagen,
                    "enlace": link,
                    "fecha_publicacion": parse_entry_date(entry, run_iso),
                    "version_id": run_id,
                    "fuente_id": src["id"],
                    "fuente_nombre": src["nombre"],
                    "ambito": src.get("ambito", "Nacional"),
                    "tipo_entidad": src.get("tipo_entidad", "Medio de Comunicación"),
                    "tema": tema,
                    "etiquetas": tags,
                })
        except Exception as exc:
            fuentes_error += 1
            print(f"[WARN] {src.get('nombre')} ({url}): {exc}")

    merged_items = new_items + existing_items
    merged_items.sort(key=lambda x: x.get("fecha_publicacion", ""), reverse=True)
    merged_items = merged_items[:MAX_ITEMS_TOTAL]

    contenidos_vinculados = build_linked_syntheses(merged_items, run_iso)

    version_entry = {
        "id": run_id,
        "etiqueta": f"{run_label} ({len(new_items)} nuevas)",
        "timestamp": run_iso,
        "total_items": len(new_items),
        "fuentes_ok": fuentes_ok,
        "fuentes_error": fuentes_error,
    }

    versions = [v for v in existing.get("versiones_disponibles", []) if v.get("id") != run_id]
    versions.insert(0, version_entry)
    versions = versions[:MAX_VERSIONS]

    output = {
        "version_etiqueta": "RSSv3",
        "ultima_actualizacion": run_iso,
        "version_actual": version_entry["etiqueta"],
        "versiones_disponibles": versions,
        "items": merged_items,
        "contenidos_vinculados": contenidos_vinculados,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(
        f"[RSSv3] Nuevas: {len(new_items)} | Total: {len(merged_items)} | "
        f"Síntesis: {len(contenidos_vinculados)} | OK: {fuentes_ok}, Err: {fuentes_error}"
    )


if __name__ == "__main__":
    main()
