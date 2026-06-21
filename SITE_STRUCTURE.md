# Target site structure (sdo.bmstu.ru)

Reconnaissance notes. The site is **Moodle** (BMSTU distance-learning system).

## 1. Authentication

- Login form: `GET https://sdo.bmstu.ru/login/index.php`
  - The page has a hidden `logintoken` field (CSRF token) — it must be parsed
    out and submitted together with the credentials.
- `POST https://sdo.bmstu.ru/login/index.php` with fields:
  `username`, `password`, `logintoken`.
- On success it redirects to `/my/`; the session is held by the `MoodleSession`
  cookie. All subsequent requests reuse a single `requests.Session`.

## 2. Course structure

- The course opens at `GET /course/view.php?id=86`.
- A specific topic: `GET /course/view.php?id=86&section=<N>`.
- The course title (used as the top-level folder name) is in the page
  `<h1>` (`.page-header-headings h1`):
  **"Продвинутый специалист по анализу больших данных (Middle data scientist)"**.
- On the course page each section is `<li class="section" data-number="N">`.
  - **17 sections found (data-number 0..16)**.
  - The section name is in an element with class `.sectionname`.

### Section map of course id=86

| data-number | Name |
|---|---|
| 0 | (service, no name) |
| 1 | Введение |
| 2 | Входное тестирование |
| 3 | 1. Рекуррентные нейронные сети. LSTM слои |
| 4 | 1.1 Сверточные нейронные сети |
| 5 | 2. Tensorflow, построение нейронных сетей на уровне графов |
| 6 | 3. Обзор библиотеки PyTorch. Особенности построения нейронных сетей |
| 7 | 4. Построение архитектуры нейронной сети для задач object detection |
| 8 | 5. Построение архитектуры нейронной сети для задач segmentation |
| 9 | 6. Задачи NLP. Препроцессинг текстовых неструктурированных данных |
| 10 | 7. Построение архитектуры нейронной сети для классификации текстов |
| 11 | 8. Чат-боты и генерация текста |
| 12 | 9. Сегментация текстовых данных |
| 13 | 10. Контроль версионности моделей с tensorflow serving |
| 14 | 11. Развёртывание облачной инфраструктуры (GCP, AWS, Sbercloud) |
| 15 | 12. flask приложение. Выведение моделей в production |
| 16 | Итоговая аттестация |

Content topics start at **section=3** (as requested) and run to the end. By
default the grabber walks sections `start_section=3 .. end_section=max`.

## 3. Section content (modules / activities)

Inside a section every item is `<li class="activity modtype_*">`:

- **`modtype_label`** — a text block. Videos are embedded here: it contains the
  rutube `<iframe>`s or HTML5 `<video>` tags. In section 3 a single label held
  **5 videos**.
- **`modtype_resource`** — a file (presentation). The activity name is in
  `.instancename` (e.g. "Презентация: Рекуррентные нейронные сети. LSTM слои").
- **`modtype_url`** — an external link (e.g. a code notebook on Google Drive).

### IMPORTANT: two different video delivery mechanisms

The course uses **two ways** of hosting videos — both inside `modtype_label`:

1. **rutube (sections 3–4)** — `<iframe src="rutube.ru/play/embed/...">`.
2. **Moodle-hosted videos (sections 5–15)** — HTML5
   `<video><source src="https://sdo.bmstu.ru/pluginfile.php/.../mod_label/intro/NNN.M.mp4">`.
   - Require an **authenticated session** (without the cookie → HTTP 407).
   - Support Range requests (resumable, status 206).
   - **Large**: ~1 GB per clip (sections 5–15 total ≈ **21.8 GB**, 26 videos).
   - The clip has no meaningful title (label text is just the editor placeholder
     "Выбрать элемент"), so the name is taken from the URL file name
     (`402.1.mp4`, etc.).

The parser must look for **both** variants, otherwise sections 5–15 appear
"empty".

Other:
- Section 6 contains a `modtype_url` module ("Ноутбук с кодом") — an external
  link to a **Google Drive file**
  (`drive.google.com/file/d/<ID>/view`, real file `11.8 PyTorch.ipynb`). We
  download the actual file with `gdown` and place it next to the videos; if the
  download fails we fall back to saving a `.url` shortcut.
- Section 16 ("Итоговая аттестация") has no videos or files.

### Videos (rutube)

- Embedded as `<iframe src="https://rutube.ru/play/embed/<ID>/?p=<TOKEN>">`.
  `<ID>` is the private video id, `<TOKEN>` (`p=`) is the private access token.
- Metadata/streams come from the API:
  `GET https://rutube.ru/api/play/options/<ID>/?no_404=true&referer=...&p=<TOKEN>`
  → JSON with `title`, `duration` and `video_balancer.m3u8` (HLS master
  playlist).
- HLS offers several qualities (e.g. 144p..**1280x720**); each variant is a
  single muxed stream (avc1 video + mp4a audio together).
- `yt-dlp` understands the private embed URL as-is, sees all qualities and picks
  the maximum with the `best` selector. It also returns the video title.

### Presentations (files)

- Resource page: `GET /mod/resource/view.php?id=<MODID>`.
  - If Moodle is set to serve directly — it responds with the file itself
    (check `Content-Type` / `Content-Disposition`).
  - Otherwise it returns HTML with a link to the real file like
    `https://sdo.bmstu.ru/pluginfile.php/.../<file name>.pdf`
    (link in `.resourceworkaround a` or any `a[href*="pluginfile.php"]`).
  - The file name is taken from the last URL segment (url-decoded).

### Linked files (url modules)

- URL module page: `GET /mod/url/view.php?id=<MODID>`. The external target is in
  `.urlworkaround a[href]` (or the first off-site link on the page).
- If the target is on Google Drive (`drive.google.com` / `docs.google.com`), the
  file id is extracted (`/d/<ID>` or `?id=<ID>`) and downloaded with `gdown`,
  which also reports the real file name.
- If the target is a plain direct file (non-HTML `Content-Type` or an
  attachment), it is streamed directly.
- Otherwise (an HTML page, a quota error, etc.) we fall back to a `.url`
  Windows shortcut.
