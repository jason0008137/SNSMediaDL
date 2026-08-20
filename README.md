English | [正體中文](README.zh-Hant.md) | [日本語](README.ja.md)

# SNSMediaDL

Pull the work of the creators you follow from X / Misskey / Mastodon / pixiv into
one local library you can browse, filter and tag.

- **Everything runs on your own machine** — nothing is uploaded, no account, no API key
- **Re-running does not re-download** — it only picks up what is new
- **Comes with a web interface** — thumbnail wall, rating and content-type tags,
  five-star rating, favorites, and one view for a creator's accounts across platforms

| Platform | How it is fetched | Login needed |
|----------|-------------------|--------------|
| X (Twitter) | Chrome extension | Yes — it uses the session already in your browser |
| Misskey | Paste the URL in the interface | No (public content) |
| Mastodon (baraag.net and friends) | Paste the URL in the interface | No (public content) |
| pixiv | Paste the URL in the interface | Yes — you supply a `PHPSESSID` |

Only actually used on Windows 10 / 11 so far.

---

## ⚠ Before you install

Read all four of these first:

- **This is a personal archiving tool, not a service.** It listens on `127.0.0.1`
  only (so only this machine can reach it) and **deliberately has no
  authentication** — anything that can reach that port can read your entire
  download history and tell it to download more. Do not expose it or forward the
  port; that publishes all of it.
- **Fetching is your responsibility.** Each platform's terms, copyright, and what
  you do with what you download are on you. This project provides no way around
  paywalls or access controls — it reads only what your already-logged-in browser
  can see anyway.
- **Do not raise the rate limits.** Fetching X too fast gets the account locked,
  and it is your account. See "Go slow" below.
- **Do not leak credentials.** The pixiv `PHPSESSID` lives in `config.toml`. That
  file is never uploaded anywhere, but do not paste its contents to anyone either.

---

## 1. What you need first

Three things; the default options are fine for all of them:

| | Where to get it | Note |
|--|-----------------|------|
| **Python 3.10 or newer** | <https://www.python.org/downloads/> | **Be sure to tick "Add Python to PATH"** in the installer |
| **Git** | <https://git-scm.com/download/win> | Needed for one-click updates |
| **Google Chrome** | <https://www.google.com/chrome/> | Only needed for X |

---

## 2. Install

### 1. Get the code

Open Command Prompt or PowerShell, go to where you want it (for example `D:\`),
and run:

```
git clone https://github.com/jason0008137/SNSMediaDL.git
cd SNSMediaDL
```

> You can also use **Code → Download ZIP** on the GitHub page instead, but then
> there is no one-click update (you can reconnect it later — see "Updating").

### 2. Start it

**Double-click `start.bat`** in the folder.

The first run installs the dependencies and takes a few minutes; later runs take
seconds. You are up when you see:

```
  API 文件   http://127.0.0.1:8765/docs
```

**Leave that black window open** — it is the program. Closing it stops the
program. Press `Ctrl + C` to stop it deliberately.

### 3. Open the interface

Go to **<http://127.0.0.1:8765/>** in your browser. That is the main screen.

### 4. Install the Chrome extension (only needed for X)

1. Type `chrome://extensions` in Chrome's address bar
2. Turn on **Developer mode** (top right)
3. Press **Load unpacked** and pick the **`extension`** folder in this project
4. Click the puzzle icon next to the address bar and pin SNSMediaDL

Click its icon; a green dot saying the backend is connected means you are done.
(A red dot usually means `start.bat` is not running.)

> ⚠ The extension's own documentation (`extension/README.md`) is currently
> **Traditional Chinese only**. That is a known gap and outside the scope of the
> three-language work.

### The three `.bat` files

| File | What it does |
|------|--------------|
| `start.bat` | Starts the program (this is the one you normally use) |
| `update.bat` | Updates to the latest version |
| `status.bat` | A quick look at how much is still waiting to download |

---

## 3. Using it

### Fetching from X

1. Make sure `start.bat` is running
2. Open `https://x.com/<account>/media` in Chrome
3. **A small round button appears in the bottom right** — click it to open the
   panel (the panel can be dragged, and it remembers where you put it)
4. **Scroll down** — the number on the panel climbs by itself; keep going until
   you are as deep as you want
5. If you want, pick a rating and a content type on the panel; this batch of posts
   is stored with those tags
6. Press **Send and download N** — the panel shows the progress down to zero

A few things worth knowing up front:

- **It only collects the account you are looking at.** Sidebar suggestions and
  whatever the home timeline scrolled past do not get mixed in.
- **One account at a time.** For the next one, switch, scroll, press again.
- **It collects even when `start.bat` is not running.** The data stays in the
  browser; press send once the program is up and it catches up.
- Pages you have already seen come from the browser cache, so scrolling may
  produce nothing new — reload the page and scroll again.

### Fetching from Misskey / Mastodon / pixiv

Web interface → the **Fetch** tab → paste the URLs (**one per line**) → look at
the parse preview → send once it looks right.

These forms are accepted:

```
https://misskey.io/@someone
https://baraag.net/@artist
https://www.pixiv.net/users/12345
@artist@baraag.net
```

- **X URLs are rejected** — those need the extension
- A URL it does not recognise is reported as such; it never guesses

**Picking up new work**: press **Refresh all** on the same page. Every tracked
account gets one incremental pass, and only what has not been fetched is fetched.

### Browsing and organising

**The Media tab** — the thumbnail wall. Filter by platform, account, rating,
content type, stars and favorites; click a thumbnail for the large view and the
details. Turn on select mode to tag a batch at once (Shift range-select and
select-page are supported).

**The Accounts tab** — set each account's default rating, and put one artist's
several accounts (across platforms, alts included) under one "creator". Once
linked, you can see all of their work at once.

**The ⚙ at the top right** — three switches you will actually use:

| Item | Default | What it does |
|------|---------|--------------|
| Background download | **Off** | When on, it works through the pending queue by itself |
| Work safe mode | **On** | The media page hides anything tagged R18, and the header says so |
| Problems & logs | — | The failure list, why each one failed, and retry |

---

## 4. Where the files go

By default `downloads\` inside the project, laid out as
`downloads\platform\account\filename`.

**To put them on another drive**: copy `config.toml.example`, rename the copy to
`config.toml` (same folder), and change this line:

```toml
output_root = 'D:\SNS_Media'
```

> Use **single quotes** for Windows paths. With double quotes, `\` is an escape
> character and the file fails to load.

Restart `start.bat` for it to take effect.

⚠️ **Changing the path does not move the old files.** They stay where they are and
their records stay valid, but for the interface to keep showing them you have to
list the old folder in `extra_media_roots` (the example file says where and how).

`config.toml` also holds the filename format, the download pacing, the pixiv
`PHPSESSID` and more. Every entry is documented next to it; leave the ones you do
not need commented out.

---

## 5. Updating

**Double-click `update.bat`.** It pulls the latest version, installs packages only
when they changed, and migrates the database only when the schema changed.

**Your data is never touched** — the downloaded files, the database and
`config.toml` are all outside the update. When the schema really does change, it
copies the database to a backup first.

- If it says the **Chrome extension changed too**: it usually reloads itself once
  the program is up; if not, press its reload button on `chrome://extensions`
- If you originally **downloaded the ZIP**: the first run offers to connect the
  update source in place — answer `y` (your data is still untouched, but code you
  changed yourself gets overwritten)
- If it says there are **unsaved changes**: you edited the code. If you do not want
  to keep the edits, run the command it prints

---

## 6. Go slow

**Fetching X too fast locks the whole account for about a day.** The defaults are
deliberately conservative (4 at a time, one second apart). Do not raise them
unless you know what you are doing.

When you really are rate limited, the program **stops that pass immediately**,
marks what it did not get as pending, and writes a line into Problems & logs.
**It does not retry by itself** — only you know when it is safe to start again.

---

## 7. Troubleshooting

| Symptom | What to do |
|---------|------------|
| `start.bat` flashes and closes | Python is missing, or "Add Python to PATH" was not ticked. Reinstall it |
| The web page does not open | Is the black `start.bat` window still open? |
| The extension shows a red "backend offline" dot | Same as above; or click the icon and check the address is `http://127.0.0.1:8765` |
| Scrolling forever and the number does not move | Check you are on the account's `/media` page (the home timeline is not collected). If the page is cached, reload and scroll again |
| The panel says in red that it cannot identify the account | Reload that tab |
| Sent successfully but no files appeared | The panel usually says why; ⚙ → Problems & logs also has it |
| Fetching stalls partway | Usually rate limiting; the log says so. Wait a while and press retry |

⚙ → **Problems & logs** is the first place to look. Every failure reason is
recorded there.

---

## 8. What it cannot do yet

- **No scheduling or polling** — fetching is something you press
- **pixiv needs your own `PHPSESSID`** (`config.toml.example` shows where), and it
  has not been tested heavily against the real site
- On X, only videos that have an mp4 are fetched; pure streaming formats are
  skipped and logged
- No transcoding, no muxing, no EXIF writing
- Deleting an account's records and re-fetching produces `xxx (1).jpg` style
  duplicates of the files that are still on disk
- The thumbnail wall gets heavy when the library is very large

---

## Licence

Apache-2.0, see `LICENSE`.
The filename format and the module split are adapted from
[twitter_media_downloader](https://github.com/Spark-NF/twitter_media_downloader)
(Spark-NF, Apache-2.0); the attribution is in `NOTICE`.
