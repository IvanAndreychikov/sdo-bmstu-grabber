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

### ⚠️ Nested sections (flexible sections format) — course id=297

Course **id=297** uses Moodle's **flexible sections** format, where sections are
**nested arbitrarily deep**. The collapsed main page shows only the top-level
sections (and reveals *none* of their inner content), so the tree must be
discovered by visiting each section page (`/course/view.php?id=297&section=N`)
and following its child subsections. For id=297 the top-level "Материалы курса"
hides a 4-level tree:

```
12 Материалы курса
└─ Модуль 1 / Модуль 2            (sections 13, 77)
   └─ Тема 1 … Тема 11            (14, 20, 26, …)
      └─ 1.1, 1.2, …              (15, 16, 17, … the leaf lessons)
```

**92 sections total** vs the 5 a flat read sees. Robust traversal
(`CourseParser.section_tree`): BFS from the top level, assign each section's
**parent on first discovery**, and **dedup** — flexsections renders a *leaf*
page in its *parent's* context (re-listing siblings), but those are already seen
and ignored. Attribute activities by their **nearest enclosing
`li.section[data-number]`** (a section page lists its siblings' activities too)
and dedup modules globally by their `module-NNN` id. Folders mirror the nesting;
top-level sections keep their section-number prefix, nested ones use sibling
order.

### ⚠️ Videos live inside `mod/page` activities — course id=297

In id=297 the lesson video is **not** inline on the section page; each lesson is
a `modtype_page` activity ("Видеоматериалы") whose `/mod/page/view.php?id=N`
**embeds 1–2 rutube players** (same private-embed mechanism as §Videos below).
The parser opens every page and extracts its rutube/direct videos. A page is
*also* saved as a document when it carries real text (≥300 chars) or has no
video, so written lessons aren't lost. `modtype_quiz` / `modtype_feedback` /
`modtype_assign` view pages are likewise captured as documents (PDF via
xhtml2pdf, HTML fallback) — **without starting any attempt/submission**, so
grades and attempt counts are untouched. Totals for id=297's materials tree:
**166 videos, 135 files, 49 docs** (28 text pages, 16 quizzes, 4 feedback, 1
assignment).

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
- **`modtype_mtslink`** — an **MTS Link (webinar.ru) webinar recording** (see
  [§4 Webinars](#4-webinars-mts-link--webinarru)). Used by course **id=297**
  ("Сила в данных: Data Science PRO"), where one section holds ~30 of them named
  "Вебинар N от DD.MM.YYYY".

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
  download fails we fall back to an OS-native link shortcut
  (`.url`/`.webloc`/`.desktop`).
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
- Otherwise (an HTML page, a quota error, etc.) we fall back to an OS-native
  link shortcut (`.url` on Windows, `.webloc` on macOS, `.desktop` on Linux).

## 4. Webinars (MTS Link / webinar.ru)

Course **id=297** delivers live webinars as `modtype_mtslink` modules. The
Moodle view page (`GET /mod/mtslink/view.php?id=<MODID>`) embeds an iframe to an
**MTS Link** (ex-webinar.ru) recording:

```
https://my.mts-link.ru/<org>/<eventId>/record-new/<eventSessionId>
```

⚠️ The **second** number is the `eventSessionId`, the first is the `eventId`.

### Resolving the media (no auth needed)

The recording page is a React SPA; `yt-dlp` has no extractor for it. The media
is resolved from a public API gateway (discovered from the SPA's `config.js`,
where `API_URL = https://gw.mts-link.ru/api`):

```
GET https://gw.mts-link.ru/api/eventsessions/<eventSessionId>/record  → JSON
```

This returns a **record descriptor**, which is an *event-log timeline*, not a
single file. Key fields:

- `duration` — length of the final (edited) video, seconds.
- `cuts` — wall-clock intervals (`{start,end}` epochs) **removed** from the
  recording (trimmed intro/outro/pauses). Identity check:
  `wall_span − Σcuts = duration`.
- `eventLogs[]` — ordered events. `relativeTime` is the position in the **edited**
  timeline (= wall offset − preceding cuts).

### Media streams

Streams are referenced by `hlsUrl` inside `mediasession` entries (in
`mediasession.add` events and in snapshots). Each is a standard HLS manifest:

```
https://events-delivery-records.webinar.ru/record/YYYY/MM/DD/<hash>.mp4/playlist.m3u8
```

A `stream` is tagged `conference` or `screensharing`:

- **conference** — the speaker's **camera + the only audio** (~540p), usually
  spanning the whole webinar.
- **screensharing** — the shared screen / slides / code (**1080p, silent**),
  shown during several intervals.

### ⚠️ One stream = many chunks (key by mediasession, not stream id)

A single logical stream (one `stream.id`) is recorded as a **sequence of
`mediasession` chunks** over time — the camera/screen is re-chunked every time it
pauses/resumes, and **each chunk has its own `hlsUrl` and `duration`**. In a
3-hour webinar one conference `stream.id` had **29 separate mediasessions**
(totalling ~83 min). Collapsing to `stream.id` (keeping the first `hlsUrl`)
silently drops every later chunk — a long talking head collapses to its first
minute. So **key the timeline by mediasession id**, and order a stream id's
chunks by capture `time`.

Per-chunk timeline placement:

- each mediasession (a `mediasession.add`, or an entry in the start snapshot)
  carries `hlsUrl`, `time` (capture epoch) and `stream.id`;
- the matching `mediasession.update` carries that chunk's final `duration`;
- a chunk spans `[time, time + duration]` in wall time, capped at the **next
  chunk of the same stream id** (the duration limits it when the stream then
  went silent before resuming);
- map wall → edited time by subtracting preceding `cuts`. A chunk that began
  during a trimmed interval needs a **source offset** (it is shown from partway
  into its own file).

Each `playlist.m3u8` downloads cleanly to mp4 with the bundled
`imageio-ffmpeg` (`ffmpeg -i <m3u8> -c copy`). See
[ARCHITECTURE.md](ARCHITECTURE.md) for how the streams are composited into one
picture-in-picture video.
