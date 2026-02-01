"""Parse AO3: user works list and comments per work. Uses requests + BeautifulSoup; optional ao3_api for work list and parallel fetch."""
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from utils import comment_hash, safe_delay

logger = logging.getLogger(__name__)

# Параллельная загрузка страниц с комментариями (батчами), чтобы не превышать лимит AO3
PARALLEL_WORKERS = 2

try:
    import AO3
    _HAS_AO3_API = True
except ImportError:
    _HAS_AO3_API = False

BASE_URL = "https://archiveofourown.org"
# Chrome User-Agent часто лучше проходит через Cloudflare, чем Firefox
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Заголовки как у браузера; Referer иногда помогает при 525
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
    "Referer": BASE_URL + "/",
}
RETRY_DELAY = 25   # секунд между повторами при 5xx
MAX_RETRIES = 3    # при 525 — не висеть дольше ~2 мин
REQUEST_TIMEOUT = 30  # секунд на один запрос, чтобы не зависать

# Selectors: AO3 uses ol.work > li.blurb, внутри — ссылка на /works/ID или works/ID
WORK_LINK_RE = re.compile(r"/?works/(\d+)")
PAGINATION_PAGE_RE = re.compile(r"[?&]page=(\d+)")


# МСК = UTC+3
MSK = timezone(timedelta(hours=3))
# Короткие названия месяцев по-русски
MONTH_RU = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
# Английские месяцы AO3 -> номер
MONTH_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# Часовой пояс в тексте AO3 -> смещение от UTC (часы)
TZ_OFFSET = {"utc": 0, "est": -5, "edt": -4, "cst": -6, "cdt": -5, "mst": -7, "mdt": -6, "pst": -8, "pdt": -7, "gmt": 0}


def _parse_ao3_text_date(raw: str) -> datetime | None:
    """Парсит формат AO3: 'Sat 31 Jan 2026 08:23 PM UTC' или '31 Jan 2026 03:23PM EST'."""
    # DD Mon YYYY HH:MM AM/PM TZ или DD Mon YYYY HH:MMAM TZ (пробел перед AM/PM необязателен)
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s*([A-Za-z]+)?", raw, re.IGNORECASE)
    if not m:
        return None
    try:
        day, mon_str, year = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        ampm = m.group(6).upper()
        tz_name = (m.group(7) or "UTC").upper()[:3]
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        month = MONTH_EN.get(mon_str)
        if not month:
            return None
        offset_hours = TZ_OFFSET.get(tz_name.lower(), 0)
        tz = timezone(timedelta(hours=offset_hours))
        dt = datetime(year, month, day, hour, minute, 0, 0, tzinfo=tz)
        return dt
    except (ValueError, KeyError):
        return None


def _normalize_date_display(raw: str) -> str:
    """Дата и время в МСК, на русском (например: 31 янв 2026, 23:23)."""
    if not raw or not raw.strip():
        return "—"
    raw = raw.strip()
    # 1) Текстовый формат AO3: "Sat 31 Jan 2026 08:23 PM UTC" или "31 Jan 2026 03:23PM EST"
    dt = _parse_ao3_text_date(raw)
    if dt is not None:
        dt_msk = dt.astimezone(MSK)
        mon = MONTH_RU[dt_msk.month - 1]
        return f"{dt_msk.day} {mon} {dt_msk.year}, {dt_msk.hour:02d}:{dt_msk.minute:02d}"
    # 2) ISO от AO3: 2026-01-31T18:14:00Z
    if "T" in raw[:30] and re.match(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            iso_core = raw[:19].replace(" ", "T")
            if len(iso_core) >= 16 and iso_core[10] in "T ":
                dt_utc = datetime.fromisoformat(iso_core.replace(" ", "T")).replace(tzinfo=timezone.utc)
                dt_msk = dt_utc.astimezone(MSK)
                mon = MONTH_RU[dt_msk.month - 1]
                return f"{dt_msk.day} {mon} {dt_msk.year}, {dt_msk.hour:02d}:{dt_msk.minute:02d}"
        except Exception:
            pass
    # 3) Не удалось распарсить — возвращаем с пробелами (склеенный вид)
    raw = re.sub(r"(\d{4})(\d{2}:\d{2})", r"\1 \2", raw)
    out = []
    for i, c in enumerate(raw):
        if i > 0 and c.isupper() and (raw[i - 1].islower() or raw[i - 1].isdigit()):
            out.append(" ")
        elif i > 0 and c.isdigit() and raw[i - 1].isalpha():
            out.append(" ")
        out.append(c)
    return "".join(out).strip() if out else raw


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def _get_with_retry(session: requests.Session, url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response | None:
    """GET с повтором при 5xx и таймауте (525 и др. часто временные)."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                logger.info("[AO3] Повтор запроса %s (попытка %s/%s)", url[:60], attempt + 1, MAX_RETRIES)
            r = session.get(url, timeout=timeout)
            if 500 <= r.status_code < 600:
                last_error = f"{r.status_code} Server Error"
                if attempt < MAX_RETRIES - 1:
                    logger.warning("[AO3] %s на %s, повтор через %s с (попытка %s/%s)", last_error, url, RETRY_DELAY, attempt + 1, MAX_RETRIES)
                    time.sleep(RETRY_DELAY)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                logger.warning("[AO3] Таймаут %s, повтор через %s с (попытка %s/%s)", url, RETRY_DELAY, attempt + 1, MAX_RETRIES)
                time.sleep(RETRY_DELAY)
        except requests.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code in (525, 502, 503):
                    logger.warning("[AO3] Ошибка %s, повтор через %s с (попытка %s/%s)", e, RETRY_DELAY, attempt + 1, MAX_RETRIES)
                    time.sleep(RETRY_DELAY)
                    continue
            raise
    if last_error is not None:
        logger.warning("[AO3] Не удалось после %s попыток: %s", MAX_RETRIES, last_error)
    return None  # 5xx или таймаут после всех повторов


def _get_works_list_url(username: str, page: int = 1) -> str:
    if page <= 1:
        return f"{BASE_URL}/users/{username}/works"
    return f"{BASE_URL}/users/{username}/works?page={page}"


def _get_work_page_url(work_id: str) -> str:
    return f"{BASE_URL}/works/{work_id}?view_full_work=true&show_comments=true"


def _collect_work_ids_and_titles_from_page(soup: BeautifulSoup, base: str) -> list[dict]:
    """Extract work_id, work_title, work_url from one works list page. AO3: ol.work > li.blurb."""
    results = []
    seen_ids = set()

    def add_work(a_tag):
        href = (a_tag.get("href") or "").strip()
        m = WORK_LINK_RE.search(href)
        if not m:
            return
        work_id = m.group(1)
        if work_id in seen_ids:
            return
        # Исключаем ссылки на "Comments", "Preview" и т.п. (часто содержат /works/ID/comments)
        if "/comments" in href or "/preview" in href.lower():
            return
        seen_ids.add(work_id)
        full_url = urljoin(base, href.split("?")[0].split("#")[0])
        title_el = a_tag.find_parent("li") or a_tag.find_parent("div") or a_tag
        heading = (title_el and title_el.find(["h4", "h3", "h2"])) or a_tag
        if hasattr(heading, "get_text"):
            title = heading.get_text(separator=" ", strip=True) or "Untitled"
        else:
            title = (a_tag.get_text(separator=" ", strip=True) if hasattr(a_tag, "get_text") else str(a_tag)) or "Untitled"
        title = re.sub(r"(\w)by(\w)", r"\1 by \2", title, flags=re.IGNORECASE)
        results.append({"work_id": work_id, "work_title": title, "work_url": full_url})

    # 1) Собираем из li.blurb (одна работа на blurb — первая ссылка на work)
    for blurb in soup.select("li.blurb"):
        for a in blurb.select("a[href*='works/']"):
            add_work(a)
            break
    # 2) Добираем все остальные ссылки на /works/ID по всей странице (на случай другой вёрстки)
    for a in soup.select("a[href*='works/']"):
        add_work(a)
    return results


def _get_works_via_api(username: str, request_delay: float) -> list[dict]:
    """Получить список работ через ao3_api Search(author=...). По 20 работ на страницу."""
    if not _HAS_AO3_API:
        return []
    all_works: list[dict] = []
    seen_ids: set[str] = set()
    page = 1
    try:
        while True:
            search = AO3.Search(author=username, page=page)
            logger.info("[AO3 API] Поиск работ по автору %s, страница %s", username, page)
            search.update()
            safe_delay(request_delay, jitter=2.0)
            if not search.results:
                break
            for work in search.results:
                work_id = str(getattr(work, "id", "") or "").strip()
                if not work_id or work_id in seen_ids:
                    continue
                seen_ids.add(work_id)
                title = getattr(work, "title", None) or "Untitled"
                work_url = f"{BASE_URL}/works/{work_id}"
                all_works.append({"work_id": work_id, "work_title": title, "work_url": work_url})
            if page >= getattr(search, "pages", 0):
                break
            page += 1
        logger.info("[AO3 API] Всего работ у пользователя %s: %s", username, len(all_works))
    except Exception as e:
        logger.warning("[AO3 API] Ошибка при получении списка работ: %s", e)
        return []
    return all_works


def _get_all_works_pages(session: requests.Session, username: str, request_delay: float) -> list[dict]:
    """Fetch all works list pages and return list of {work_id, work_title, work_url}."""
    all_works: list[dict] = []
    seen_ids: set[str] = set()
    page = 1
    while True:
        url = _get_works_list_url(username, page)
        logger.info("[AO3] Загрузка списка работ, страница %s: %s", page, url)
        try:
            r = _get_with_retry(session, url)
        except requests.RequestException as e:
            logger.warning("Не удалось загрузить список работ %s: %s", url, e)
            break
        if r is None:
            logger.warning("Не удалось загрузить список работ %s после %s попыток (525/5xx)", url, MAX_RETRIES)
            break
        soup = BeautifulSoup(r.text, "html.parser")
        base = r.url
        works_on_page = _collect_work_ids_and_titles_from_page(soup, base)
        logger.info("[AO3] На странице %s найдено работ: %s", page, len(works_on_page))
        if not works_on_page:
            # Сохраняем ответ для отладки — откройте ao3_last_page.html и проверьте разметку
            try:
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ao3_last_page.html")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(r.text or "")
                logger.warning("[AO3] Работ не найдено. Ответ сохранён в ao3_last_page.html — откройте файл и проверьте, что вернул сервер (Cloudflare? полный HTML?).")
            except OSError:
                pass
            body_lower = (r.text or "")[:2000].lower()
            if "just a moment" in body_lower or "cloudflare" in body_lower or "challenge" in body_lower:
                logger.warning("[AO3] Похоже на страницу проверки Cloudflare. Увеличьте REQUEST_DELAY в config.ini.")
            elif len(r.text or "") < 5000:
                logger.warning("[AO3] Страница очень короткая (%s символов).", len(r.text or ""))
            break
        for w in works_on_page:
            if w["work_id"] not in seen_ids:
                seen_ids.add(w["work_id"])
                all_works.append(w)
        # Next page: pagination links
        next_page = None
        for a in soup.select(".pagination a[href*='page=']"):
            href = a.get("href") or ""
            mo = PAGINATION_PAGE_RE.search(href)
            if mo:
                p = int(mo.group(1))
                if p == page + 1:
                    next_page = p
                    break
        if next_page is None:
            break
        page = next_page
        safe_delay(request_delay, jitter=2.0)
    logger.info("[AO3] Всего работ у пользователя %s: %s", username, len(all_works))
    return all_works


def _parse_comment_block(block, work_id: str, work_title: str, work_url: str) -> dict | None:
    """Parse one comment block into {work_id, work_title, work_url, author, date, text, comment_id}."""
    comment_id = block.get("data-comment-id")
    # Author: often .heading a or .byline a, or "Anonymous"
    author_el = block.select_one(".heading a, .byline a, .comment .user a")
    author = author_el.get_text(strip=True) if author_el else "Anonymous"
    # Date: datetime or .datetime
    date_el = block.select_one("time[datetime], .datetime, .posted")
    if date_el and date_el.get("datetime"):
        raw_date = date_el["datetime"]
    elif date_el:
        raw_date = date_el.get_text(separator=" ", strip=True)
    else:
        raw_date = ""
    # Нормализуем дату: пробелы между частями (Sat 31 Jan 2026 06:14 PM)
    date_str = _normalize_date_display(raw_date)
    # Text: .userstuff or .comment-body or block text
    text_el = block.select_one(".userstuff, .comment-body, blockquote")
    text = text_el.get_text(separator="\n", strip=True) if text_el else block.get_text(separator="\n", strip=True)
    if not comment_id:
        comment_id = comment_hash(author, date_str, (text or "")[:200])
    return {
        "work_id": work_id,
        "work_title": work_title,
        "work_url": work_url,
        "author": author or "Anonymous",
        "date": date_str or "—",
        "text": text or "",
        "comment_id": comment_id,
    }


def _get_comments_from_work_page(
    session: requests.Session,
    work_id: str,
    work_title: str,
    work_url: str,
) -> list[dict]:
    """Fetch one work page with comments and return list of comment dicts."""
    url = _get_work_page_url(work_id)
    logger.info("[AO3] Загрузка работы %s: %s", work_id, work_title[:50])
    try:
        r = _get_with_retry(session, url)
    except requests.RequestException as e:
        logger.warning("Не удалось загрузить работу %s: %s", work_id, e)
        return []
    if r is None:
        logger.warning("Не удалось загрузить работу %s после %s попыток (525/5xx)", work_id, MAX_RETRIES)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    comments = []
    blocks = soup.select("[data-comment-id]")
    if not blocks:
        blocks = soup.select(".comment")
    for block in blocks:
        c = _parse_comment_block(block, work_id, work_title, work_url)
        if c:
            comments.append(c)
    logger.info("[AO3] Работа %s: комментариев %s", work_id, len(comments))
    return comments


def _fetch_comments_for_work(work: dict, request_delay: float) -> list[dict]:
    """Загрузить комментарии одной работы (собственная сессия для потока)."""
    session = _session()
    safe_delay(request_delay, jitter=1.0)
    return _get_comments_from_work_page(
        session, work["work_id"], work["work_title"], work["work_url"]
    )


def get_all_comments_for_user(username: str, request_delay: float = 7.0) -> list[dict]:
    """
    Fetch all works for user, then all comments for each work.
    Uses ao3_api Search(author=...) for work list if available; fetches comment pages in parallel (batches of PARALLEL_WORKERS).
    Returns list of comment dicts: work_id, work_title, work_url, author, date, text, comment_id.
    """
    logger.info("[AO3] Начинаем сбор комментариев для пользователя %s", username)
    if _HAS_AO3_API:
        works = _get_works_via_api(username, request_delay)
        if works:
            logger.info("[AO3] Список работ получен через ao3_api (%s работ)", len(works))
    if not _HAS_AO3_API or not works:
        session = _session()
        works = _get_all_works_pages(session, username, request_delay)
    if not works:
        logger.warning("[AO3] Работ не найдено для пользователя %s", username)
        return []
    all_comments: list[dict] = []
    workers = min(PARALLEL_WORKERS, len(works))
    if workers <= 1:
        session = _session()
        for w in works:
            safe_delay(request_delay, jitter=2.0)
            comments = _get_comments_from_work_page(session, w["work_id"], w["work_title"], w["work_url"])
            all_comments.extend(comments)
    else:
        # Батчами: PARALLEL_WORKERS работ параллельно, затем пауза request_delay
        for i in range(0, len(works), workers):
            batch = works[i : i + workers]
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = [executor.submit(_fetch_comments_for_work, w, 0) for w in batch]
                for future in as_completed(futures):
                    try:
                        all_comments.extend(future.result())
                    except Exception as e:
                        logger.warning("[AO3] Ошибка загрузки комментариев: %s", e)
            if i + workers < len(works):
                safe_delay(request_delay, jitter=2.0)
    logger.info("[AO3] Всего комментариев: %s", len(all_comments))
    return all_comments
