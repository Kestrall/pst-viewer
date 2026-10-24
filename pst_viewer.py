#!/usr/bin/env python3
"""PST Viewer : parcourir les dossiers et les mails d'un fichier Outlook .pst.

Le .pst est extrait une seule fois avec readpst (paquet pst-utils), indexé
dans SQLite (recherche plein texte FTS5), puis servi par une petite interface
web locale. Aucune dépendance Python hors bibliothèque standard.

    python3 pst_viewer.py                          # écran d'accueil pour choisir le .pst
    python3 pst_viewer.py ~/Documents/archive.pst  # ouverture directe
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from concurrent.futures import ProcessPoolExecutor
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pst-viewer"
INDEX_VERSION = 2
MAX_INDEXED_TEXT = 20000


# --------------------------------------------------------------------------
# Lecture des messages
# --------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Transforme un corps HTML en texte brut (pour l'index et l'aperçu)."""

    SKIP = {"script", "style", "head", "title"}
    BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup):
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def part_text(part):
    """Contenu texte d'une partie MIME, robuste aux charsets exotiques."""
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("cp1252", "replace")


def read_message(path):
    with open(path, "rb") as fh:
        return BytesParser(policy=policy.default).parse(fh)


def header(msg, name):
    try:
        value = msg.get(name)
        return str(value).strip() if value is not None else ""
    except Exception:
        raw = msg.get_all(name, failobj=[""])[0]
        return str(raw).strip()


def bodies(msg):
    """Renvoie (texte, html) du message ; l'un ou l'autre peut être vide."""
    text = html_body = ""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            text = part_text(part)
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            html_body = part_text(part)
    except Exception:
        pass
    if not text and not html_body and not msg.is_multipart():
        if msg.get_content_type() == "text/html":
            html_body = part_text(msg)
        else:
            text = part_text(msg)
    return text, html_body


def attachments(msg):
    """Liste des pièces jointes : [(partie, nom, type, taille, content-id)]."""
    found = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        found.append((part, filename or f"piece-jointe-{len(found) + 1}",
                      part.get_content_type(), len(payload), cid))
    return found


def parse_date(value):
    try:
        dt = parsedate_to_datetime(value)
        return int(dt.timestamp()) if dt else None
    except Exception:
        return None


def ics_fields(path):
    """Champs d'un événement .ics produit par readpst (format simple)."""
    raw = Path(path).read_text("utf-8", "replace")
    raw = re.sub(r"\r?\n[ \t]", "", raw)  # lignes repliées
    fields = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.split(";", 1)[0].upper()
        if key not in fields:
            fields[key] = (value.replace("\\n", "\n").replace("\\,", ",")
                           .replace("\\;", ";").replace("\\\\", "\\"))
    return fields


def ics_timestamp(value):
    from datetime import datetime, timezone
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if fmt.endswith("Z"):
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            continue
    return None


def index_file(path):
    """Extrait les métadonnées d'un fichier (exécuté dans un sous-processus)."""
    try:
        size = os.path.getsize(path)
        if path.endswith(".ics"):
            f = ics_fields(path)
            body = "\n".join(filter(None, [f.get("LOCATION"), f.get("DESCRIPTION")]))
            return dict(path=path, kind="event", subject=f.get("SUMMARY", ""),
                        sender=f.get("ORGANIZER", "").replace("MAILTO:", ""),
                        recipients="", date=ics_timestamp(f.get("DTSTART")),
                        size=size, has_att=0, snippet=body[:200], body=body)
        msg = read_message(path)
        text, html_body = bodies(msg)
        if not text and html_body:
            text = html_to_text(html_body)
        has_att = any(a[0].get_content_disposition() == "attachment" or not a[4]
                      for a in attachments(msg))
        recipients = ", ".join(filter(None, (header(msg, "To"), header(msg, "Cc"))))
        snippet = re.sub(r"\s+", " ", text).strip()[:200]
        return dict(path=path, kind="mail", subject=header(msg, "Subject"),
                    sender=header(msg, "From"), recipients=recipients,
                    date=parse_date(header(msg, "Date")), size=size,
                    has_att=int(has_att), snippet=snippet,
                    body=text[:MAX_INDEXED_TEXT])
    except Exception as exc:  # un message corrompu ne bloque pas l'index
        return dict(path=path, kind="mail", subject=f"(illisible : {exc})",
                    sender="", recipients="", date=None, size=0, has_att=0,
                    snippet="", body="")


# --------------------------------------------------------------------------
# Extraction et indexation
# --------------------------------------------------------------------------

def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


class Archive:
    """Un fichier .pst extrait et indexé dans le cache."""

    def __init__(self, pst_path):
        self.pst = Path(pst_path).expanduser().resolve()
        stat = self.pst.stat()
        key = hashlib.sha1(f"{self.pst}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:16]
        self.dir = CACHE_ROOT / key
        self.mail_dir = self.dir / "mail"
        self.db_path = self.dir / "index.sqlite"
        self.ready_flag = self.dir / f"ready-v{INDEX_VERSION}"
        self._local = threading.local()
        # Avancement, lu par l'interface pendant la préparation
        self.state = "ready" if self.ready_flag.exists() else "pending"
        self.done = self.total = 0
        self.error = ""

    # -- connexion SQLite par thread du serveur
    @property
    def db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def status(self):
        return {"state": self.state, "done": self.done, "total": self.total, "error": self.error}

    def prepare(self, rebuild=False):
        """Extrait et indexe le .pst si besoin (à lancer dans un thread)."""
        try:
            if rebuild and self.dir.exists():
                shutil.rmtree(self.dir)
            if self.ready_flag.exists():
                self.state = "ready"
                return
            if self.mail_dir.exists():
                shutil.rmtree(self.mail_dir)
            self.db_path.unlink(missing_ok=True)
            self.mail_dir.mkdir(parents=True)
            self.extract()
            self.build_index()
            self.ready_flag.write_text(str(self.pst))
            self.state = "ready"
            print(f"{self.pst.name} : {self.total} éléments indexés.")
        except Exception as exc:
            self.state, self.error = "error", str(exc)
            print(f"Erreur sur {self.pst.name} : {exc}", file=sys.stderr)

    def extract(self):
        if shutil.which("readpst") is None:
            raise RuntimeError("readpst est introuvable : installez-le avec « sudo apt install pst-utils ».")
        self.state = "extracting"
        print(f"[1/2] Extraction de {self.pst.name} (readpst)…")
        # -j 0 : le mode parallèle de readpst (actif par défaut) saute des
        # messages au hasard ; en séquentiel l'extraction est complète et stable.
        cmd = ["readpst", "-e", "-b", "-q", "-j", "0", "-o", str(self.mail_dir), str(self.pst)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            # readpst n'affiche pas d'avancement : on compte les fichiers écrits
            self.done = sum(len(files) for _, _, files in os.walk(self.mail_dir))
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                pass
        if proc.returncode != 0:
            raise RuntimeError(f"readpst a échoué : {proc.stderr.read().strip() or proc.returncode}")

    def build_index(self):
        files = sorted(str(p) for p in self.mail_dir.rglob("*")
                       if p.is_file() and p.suffix in (".eml", ".ics"))
        self.state, self.done, self.total = "indexing", 0, len(files)
        print(f"[2/2] Indexation de {len(files)} éléments…")
        db = sqlite3.connect(self.db_path)
        db.executescript("""
            CREATE TABLE folders(id INTEGER PRIMARY KEY, parent INTEGER, name TEXT,
                                 path TEXT UNIQUE, count INTEGER DEFAULT 0, total INTEGER DEFAULT 0);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, folder INTEGER, path TEXT,
                                  kind TEXT, subject TEXT, sender TEXT, recipients TEXT,
                                  date INTEGER, size INTEGER, has_att INTEGER, snippet TEXT);
            CREATE INDEX messages_folder_date ON messages(folder, date);
            CREATE VIRTUAL TABLE search USING fts5(subject, sender, recipients, body,
                                                   tokenize='unicode61 remove_diacritics 2');
        """)
        folder_ids = {}

        def folder_id(rel):
            if rel in folder_ids:
                return folder_ids[rel]
            parent = folder_id(str(Path(rel).parent)) if "/" in rel else None
            cur = db.execute("INSERT INTO folders(parent, name, path) VALUES (?,?,?)",
                             (parent, Path(rel).name, rel))
            folder_ids[rel] = cur.lastrowid
            return cur.lastrowid

        for d in sorted((p for p in self.mail_dir.rglob("*") if p.is_dir()),
                        key=lambda p: natural_key(str(p))):
            folder_id(str(d.relative_to(self.mail_dir)))

        with ProcessPoolExecutor() as pool:
            for item in pool.map(index_file, files, chunksize=64):
                rel = str(Path(item["path"]).parent.relative_to(self.mail_dir))
                cur = db.execute(
                    "INSERT INTO messages(folder, path, kind, subject, sender, recipients,"
                    " date, size, has_att, snippet) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (folder_id(rel), str(Path(item["path"]).relative_to(self.mail_dir)),
                     item["kind"], item["subject"], item["sender"], item["recipients"],
                     item["date"], item["size"], item["has_att"], item["snippet"]))
                db.execute("INSERT INTO search(rowid, subject, sender, recipients, body)"
                           " VALUES (?,?,?,?,?)",
                           (cur.lastrowid, item["subject"], item["sender"],
                            item["recipients"], item["body"]))
                self.done += 1

        db.execute("UPDATE folders SET count = (SELECT COUNT(*) FROM messages m WHERE m.folder = folders.id)")
        db.execute("""UPDATE folders SET total = (SELECT COUNT(*) FROM messages m
                      JOIN folders f ON f.id = m.folder
                      WHERE f.path = folders.path OR f.path LIKE folders.path || '/%')""")
        db.commit()
        db.close()

    # -- requêtes
    def folders(self):
        rows = self.db.execute("SELECT id, parent, name, count, total FROM folders").fetchall()
        # Dossiers Outlook d'abord, puis les dossiers d'archive par année (récentes en tête)
        rows = sorted(rows, key=lambda r: (r["name"].isdigit(),
                                           -int(r["name"]) if r["name"].isdigit() else 0,
                                           natural_key(r["name"])))
        return [dict(r) for r in rows]

    def messages(self, folder=None, query=None, offset=0, limit=100, recursive=False):
        where, args = [], []
        if folder:
            if recursive:
                path = self.db.execute("SELECT path FROM folders WHERE id=?", (folder,)).fetchone()
                where.append("m.folder IN (SELECT id FROM folders WHERE path = ? OR path LIKE ? || '/%')")
                args += [path["path"] if path else "", path["path"] if path else ""]
            else:
                where.append("m.folder = ?")
                args.append(folder)
        join = ""
        if query:
            join = "JOIN search s ON s.rowid = m.id"
            where.append("search MATCH ?")
            args.append(fts_query(query))
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        try:
            total = self.db.execute(f"SELECT COUNT(*) FROM messages m {join} {sql_where}", args).fetchone()[0]
            rows = self.db.execute(
                f"SELECT m.id, m.kind, m.subject, m.sender, m.recipients, m.date, m.size,"
                f" m.has_att, m.snippet, f.name AS folder_name FROM messages m {join}"
                f" JOIN folders f ON f.id = m.folder {sql_where}"
                f" ORDER BY m.date IS NULL, m.date DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        except sqlite3.OperationalError:
            return {"total": 0, "items": []}
        return {"total": total, "items": [dict(r) for r in rows]}

    def message_file(self, msg_id):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if row is None:
            return None, None
        path = (self.mail_dir / row["path"]).resolve()
        if self.mail_dir.resolve() not in path.parents:
            return None, None
        return row, path


def fts_query(text):
    """Requête utilisateur -> requête FTS5 sûre (chaque mot en préfixe)."""
    words = re.findall(r"\w+", text, re.UNICODE)
    return " ".join(f'"{w}"*' for w in words) or '""'


def short_address(value):
    pairs = getaddresses([value]) if value else []
    return [{"name": n, "email": e} for n, e in pairs if n or e]


# --------------------------------------------------------------------------
# Serveur web
# --------------------------------------------------------------------------

def html_csp(remote_images):
    img = "'self' data:" + (" https: http:" if remote_images else "")
    return (f"default-src 'none'; img-src {img}; style-src 'unsafe-inline' 'self'; "
            "font-src data:; base-uri 'none'; form-action 'none'")


_archives_lock = threading.Lock()
ARCHIVES = []  # archives ouvertes pendant la session ; l'indice sert d'identifiant


def open_archive(path, rebuild=False):
    """Ouvre un .pst (ou renvoie celui déjà ouvert) et lance sa préparation."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"Fichier introuvable : {path}")
    if resolved.suffix.lower() != ".pst":
        raise ValueError("Ce fichier n'est pas une archive .pst.")
    with _archives_lock:
        for i, arc in enumerate(ARCHIVES):
            if arc.pst == resolved and arc.state != "error":
                return i
        arc = Archive(resolved)
        ARCHIVES.append(arc)
        idx = len(ARCHIVES) - 1
    if arc.ready_flag.exists() and not rebuild:
        arc.ready_flag.touch()  # date de dernière ouverture, pour « Archives récentes »
    if arc.state != "ready" or rebuild:
        arc.state = "pending"
        threading.Thread(target=arc.prepare, args=(rebuild,), daemon=True).start()
    return idx


def recent_archives():
    """Archives déjà indexées dans le cache (ouverture instantanée)."""
    found = []
    for flag in CACHE_ROOT.glob(f"*/ready-v{INDEX_VERSION}"):
        pst = Path(flag.read_text().strip())
        if pst.is_file():
            found.append({"name": pst.name, "path": str(pst), "size": pst.stat().st_size,
                          "opened": int(flag.stat().st_mtime)})
    return sorted(found, key=lambda a: -a["opened"])


def browse(path):
    """Contenu d'un dossier local : sous-dossiers et fichiers .pst uniquement."""
    folder = Path(path or Path.home()).expanduser().resolve()
    if not folder.is_dir():
        folder = Path.home()
    entries = []
    try:
        children = list(folder.iterdir())
    except PermissionError:
        children = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child), "type": "dir"})
            elif child.suffix.lower() == ".pst":
                entries.append({"name": child.name, "path": str(child), "type": "pst",
                                "size": child.stat().st_size})
        except OSError:
            continue
    entries.sort(key=lambda e: (e["type"] != "pst", natural_key(e["name"])))
    home = Path.home()
    places = [("Dossier personnel", home), ("Documents", home / "Documents"),
              ("Téléchargements", home / "Téléchargements"), ("Téléchargements", home / "Downloads"),
              ("Bureau", home / "Bureau"), ("Bureau", home / "Desktop"),
              ("Supports amovibles", Path("/media") / home.name), ("Racine", Path("/"))]
    seen = set()
    return {"path": str(folder), "parent": str(folder.parent) if folder != folder.parent else None,
            "entries": entries,
            "places": [{"name": n, "path": str(p)} for n, p in places
                       if p.is_dir() and n not in seen and not seen.add(n)]}


class Handler(BaseHTTPRequestHandler):
    allowed_hosts = set()  # renseigné au démarrage (protection DNS rebinding)

    def log_message(self, fmt, *args):
        pass

    # -- utilitaires
    def send(self, body, ctype="application/json; charset=utf-8", status=200, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=200):
        self.send(json.dumps(data, ensure_ascii=False), status=status)

    def not_found(self):
        self.json({"error": "introuvable"}, HTTPStatus.NOT_FOUND)

    def host_ok(self):
        if self.headers.get("Host", "") in self.allowed_hosts:
            return True
        self.json({"error": "hôte refusé"}, HTTPStatus.FORBIDDEN)
        return False

    def archive(self, idx):
        try:
            return ARCHIVES[int(idx)]
        except (ValueError, IndexError):
            return None

    @staticmethod
    def describe(i, arc):
        return {"id": i, "name": arc.pst.name, "path": str(arc.pst),
                "size": arc.pst.stat().st_size, **arc.status()}

    # -- routes
    def do_POST(self):
        if not self.host_ok():
            return
        # Corps JSON obligatoire : un formulaire d'un site tiers ne peut pas l'envoyer
        if urlparse(self.path).path != "/api/open" or \
                not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.not_found()
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 65536)
            data = json.loads(self.rfile.read(length) or b"{}")
            idx = open_archive(str(data.get("path", "")), rebuild=bool(data.get("rebuild")))
        except (ValueError, OSError) as exc:
            return self.json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self.json(self.describe(idx, ARCHIVES[idx]))

    def do_GET(self):
        if not self.host_ok():
            return
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]

        if not parts:
            return self.static("index.html")
        if parts[0] == "static" and len(parts) == 2:
            return self.static(parts[1])
        if parts[0] != "api":
            return self.not_found()

        if parts[1:] == ["archives"]:
            return self.json([self.describe(i, a) for i, a in enumerate(ARCHIVES)])
        if parts[1:] == ["recent"]:
            return self.json(recent_archives())
        if parts[1:] == ["browse"]:
            return self.json(browse(params.get("path")))

        if len(parts) < 3 or (arc := self.archive(parts[1])) is None:
            return self.not_found()

        if parts[2] == "status":
            return self.json(self.describe(int(parts[1]), arc))
        if arc.state != "ready":
            return self.json({"error": "archive en cours de préparation"}, HTTPStatus.CONFLICT)

        if parts[2] == "folders":
            return self.json(arc.folders())

        if parts[2] == "messages":
            return self.json(arc.messages(
                folder=int(params["folder"]) if params.get("folder", "").isdigit() else None,
                query=params.get("q", "").strip() or None,
                offset=max(0, int(params.get("offset", 0) or 0)),
                limit=min(500, int(params.get("limit", 100) or 100)),
                recursive=params.get("recursive") == "1"))

        if parts[2] == "message" and len(parts) >= 4 and parts[3].isdigit():
            row, path = arc.message_file(int(parts[3]))
            if row is None or not path.exists():
                return self.not_found()
            action = parts[4] if len(parts) > 4 else "info"
            return self.message_route(arc, row, path, action, parts[5:], params)

        return self.not_found()

    def static(self, name):
        path = (APP_DIR / "static" / name).resolve()
        if path.parent != (APP_DIR / "static").resolve() or not path.is_file():
            return self.not_found()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self.send(path.read_bytes(), ctype)

    def message_route(self, arc, row, path, action, rest, params):
        base = f"/api/{ARCHIVES.index(arc)}/message/{row['id']}"

        if row["kind"] == "event":
            if action != "info":
                return self.not_found()
            f = ics_fields(path)
            return self.json({"id": row["id"], "kind": "event", "subject": f.get("SUMMARY", ""),
                              "start": ics_timestamp(f.get("DTSTART")),
                              "end": ics_timestamp(f.get("DTEND")),
                              "location": f.get("LOCATION", ""),
                              "organizer": f.get("ORGANIZER", "").replace("MAILTO:", ""),
                              "text": f.get("DESCRIPTION", ""), "html": False,
                              "attachments": [], "raw": None})

        msg = read_message(path)
        if action == "info":
            text, html_body = bodies(msg)
            atts = [{"index": i, "name": name, "type": ctype, "size": size,
                     "url": f"{base}/att/{i}"}
                    for i, (_, name, ctype, size, cid) in enumerate(attachments(msg))
                    if not (cid and html_body and cid in html_body)]
            return self.json({
                "id": row["id"], "kind": "mail",
                "subject": header(msg, "Subject"),
                "from": short_address(header(msg, "From")),
                "to": short_address(header(msg, "To")),
                "cc": short_address(header(msg, "Cc")),
                "date": row["date"], "date_raw": header(msg, "Date"),
                "text": text, "html": bool(html_body),
                "html_url": f"{base}/html", "raw": f"{base}/raw",
                "attachments": atts,
                "headers": [[k, str(v)] for k, v in msg.raw_items()],
            })

        if action == "html":
            _, html_body = bodies(msg)
            if not html_body:
                return self.not_found()
            cids = {cid: i for i, (_, _, _, _, cid) in enumerate(attachments(msg)) if cid}
            html_body = re.sub(
                r"cid:([^\"'\s)>]+)",
                lambda m: f"{base}/att/{cids[unquote(m.group(1))]}"
                if unquote(m.group(1)) in cids else m.group(0), html_body)
            # Liens ouverts hors de l'iframe ; pas de script (CSP + sandbox côté client)
            inject = '<base target="_blank"><meta name="referrer" content="no-referrer">'
            if re.search(r"<head[^>]*>", html_body, re.I):
                html_body = re.sub(r"(<head[^>]*>)", r"\1" + inject, html_body, count=1, flags=re.I)
            else:
                html_body = inject + html_body
            return self.send(html_body, "text/html; charset=utf-8", headers={
                "Content-Security-Policy": html_csp(params.get("remote") == "1")})

        if action == "att" and rest and rest[0].isdigit():
            found = attachments(msg)
            idx = int(rest[0])
            if idx >= len(found):
                return self.not_found()
            part, name, ctype, _, _ = found[idx]
            inline = ctype.startswith("image/") and ctype != "image/svg+xml"
            disposition = "inline" if inline else "attachment"
            return self.send(part.get_payload(decode=True) or b"",
                             ctype if inline else "application/octet-stream",
                             headers={"Content-Disposition":
                                      f"{disposition}; filename*=UTF-8''{quote(name)}"})

        if action == "raw":
            name = re.sub(r"[^\w.-]+", "_", header(msg, "Subject") or "message")[:80] + ".eml"
            return self.send(path.read_bytes(), "message/rfc822", headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

        return self.not_found()


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Visionneuse de fichiers Outlook .pst")
    parser.add_argument("pst", nargs="*", help="fichier(s) .pst à ouvrir directement (facultatif)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--rebuild", action="store_true", help="réextraire et réindexer")
    parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    args = parser.parse_args()

    for p in args.pst:
        try:
            open_archive(p, rebuild=args.rebuild)
        except ValueError as exc:
            sys.exit(str(exc))

    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/" + ("#/archive/0" if args.pst else "")
    print(f"PST Viewer prêt : {url}  (Ctrl+C pour quitter)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAu revoir.")


if __name__ == "__main__":
    main()
