// 設定面板（header 的齒輪）。
//
// 這裡是**系統模型的家**。2.0 之前，「背景下載開著會發生什麼、它與 extension
// 的『送出並下載』有什麼不同」這段話被塞在一個 `title` 屬性裡 —— hover 一秒
// 才出現、不能換行、鍵盤與觸控都拿不到。那是整個系統模型的核心，現在是
// 這個面板裡的一等公民文字。
//
// ⚠️ 設定不是工作面，所以它不佔一級位置；但它也不准縮成一排沒有說明的開關。

import { esc, hint, singleDrop } from '../dom.js';
import { api } from '../api.js';
import { state, safeMode, setSafeMode, onSafeModeChange } from '../state.js';
import { openOverlay } from '../overlay.js';
import { LANGS, LANG_NAMES, currentLang, fmt, t } from '../i18n.js';
import { loadSettings, patchSetting, resetSetting, setAutoDownload } from '../queue.js';

/** pixiv 憑證：**只說有沒有設，永遠不顯示值的任何片段。**
 *
 *  沒設的時候要說「會怎樣 + 怎麼填」。只寫「未設定」的話，使用者仍然
 *  會去抓一輪，然後撞上一個看起來像 Cloudflare 擋人的 403。 */
function credentialRow(s) {
  const has = s.credentials?.pixiv;
  if (has) return t('settings.cred.set');
  // 「一定會失敗」是**約束**，留在畫面上；填法是格式規則，進氣泡。
  return `<span class="warn">${esc(t('settings.cred.unset'))}</span>${
    esc(t('settings.cred.unset.rest'))}${hint(
t('settings.cred.tip'))}`;
}

/** 偵測到的來源。**一定要講出來，不能只說「已安裝」** ——
 *  三層偵測（你指定的 / 系統 PATH / pip 帶的）命中哪一層，決定了你在用
 *  哪個版本的 ffmpeg，而那正是「這個檔為什麼抽不出影格」的第一個問題。 */
// ⚠️ 值是 key 不是文字：這張表在模組載入時就建好，那時 i18n 還沒載完。
const FFMPEG_SOURCE = {
  config: 'settings.ffmpeg.config',
  path: 'settings.ffmpeg.path',
  bundled: 'settings.ffmpeg.bundled',
};

/** ffmpeg 偵測結果。沒裝的時候要說**影響範圍** ——
 *  「未安裝」三個字不足以讓人判斷要不要去裝。 */
function ffmpegRow(s) {
  const f = s.ffmpeg;
  if (!f) return t('settings.ffmpeg.noreport');
  if (!f.available) {
    // 兩句都是**作用範圍**，都留。只有「格線會寫出原因」是補充，進氣泡。
    return `<span class="warn">${esc(t('settings.ffmpeg.unset'))}</span>${
       esc(t('settings.ffmpeg.unset.rest'))}<br>
       ${t('settings.ffmpeg.unaffected', {
         what: `<b>${esc(t('settings.ffmpeg.unaffected.b'))}</b>`,
       })}${hint(t('settings.ffmpeg.tip'))}`;
  }
  // 來源不認得時就不硬掰一個標籤 —— 路徑本身仍然是完整的答案。
  const from = FFMPEG_SOURCE[f.source];
  return `${esc(f.path)}${from
    ? `<br><span class="note">${esc(t('settings.ffmpeg.from', { src: t(from) }))}</span>`
    : ''}`;
}

/** 這個值是誰決定的。**沿用 ffmpeg 那一套**（回報命中哪一層），不新發明。
 *
 *  只有 `prefs` 需要多說一句：它是唯一「你自己按出來、而且會蓋掉 config.toml」
 *  的來源，也是唯一答得出「我改了 config.toml 為什麼沒生效」的地方。 */
// ⚠️ 值是 key 不是文字（模組載入時 i18n 還沒載完）。
// `config` 的值三個語系都是「config.toml」——那是檔名 —— 但仍然走語系檔，
// 因為少一個 key 就會讓 `SOURCE_TEXT[src]` 落空、畫面上出現裸的 'config'。
const SOURCE_TEXT = {
  prefs: 'settings.source.prefs',
  config: 'settings.source.configfile',
  env: 'settings.source.env',
  default: 'settings.source.default',
};

const onOff = (v) => t(v ? 'common.on' : 'common.off');

/** 來源那一行。⚠️ **沒有衝突時回空字串** —— 沒被覆蓋的設定多一行「來源：預設值」
 *  是純雜訊，而雜訊會讓真正有衝突的那一次被滑過去。 */
function sourceLine(s, key, show = String) {
  if (!s?.sources) return '';
  const src = s.sources[key];
  const conflict = s.config_values && key in s.config_values;
  if (!conflict) {
    // 環境變數是唯一「你在 GUI 上改不動」的來源，那要講；其餘不囉唆。
    return src === 'env'
      ? `<p class="note">${esc(t('settings.source.env.locked'))}</p>`
      : '';
  }
  return `<p class="note">${esc(t('settings.source.now', {
      value: show(s[key]),
      src: SOURCE_TEXT[src] ? t(SOURCE_TEXT[src]) : src,
    }))}
    ${esc(t('settings.source.config', { value: show(s.config_values[key]) }))}
    <button type="button" class="linkish" data-reset="${key}">${
      esc(t('settings.source.reset'))}</button></p>`;
}

function readonlyRows(s) {
  if (!s) return `<div class="err">${esc(t('settings.readonly.unavailable'))}</div>`;
  const extra = s.extra_media_roots || [];
  return `
    <dl class="kv">
      <dt>${esc(t('settings.readonly.output'))}</dt><dd>${esc(s.output_root)}</dd>
      <dt>${esc(t('settings.readonly.thumb'))}</dt><dd>${esc(s.thumb_root)}</dd>
      <dt>${esc(t('settings.readonly.extra'))}</dt><dd>${extra.length
        ? `${esc(t('settings.readonly.extra.n', { n: fmt.num(extra.length) }))}<br>${
          extra.map(esc).join('<br>')}`
        : esc(t('settings.readonly.extra.none'))}</dd>
      <dt>${esc(t('settings.readonly.maxpages'))}</dt><dd>${
        esc(t('settings.readonly.maxpages.v', { n: fmt.num(s.fetch_max_pages) }))}</dd>
      <dt>${esc(t('settings.readonly.cred'))}</dt><dd>${credentialRow(s)}</dd>
      <dt>ffmpeg</dt><dd>${ffmpegRow(s)}</dd>
    </dl>
    <p class="note">${esc(t('settings.restart'))}${hint(t('settings.restart.tip'))}</p>`;
}

export async function openSettings() {
  const handle = openOverlay({
    title: t('settings.title'),
    body: `
      <!-- 偏好檔壞掉時的警示。⚠️ 使用者看到的症狀是「我的設定不見了」，
           那句話要出現在他會去看的地方，不是只進 console。 -->
      <div id="setPrefsErr" class="ovl-section danger-zone hidden"></div>

      <!-- 語言放最上面：它決定其他每一個字長什麼樣，層級高於所有設定。
           三個選項各用**該語言自己的名字** —— 看不懂目前介面語言的人，
           正是最需要在這裡找到自己語言的那個人。 -->
      <div class="ovl-section">
        <h3 data-i18n="settings.language.title">Language</h3>
        <span id="setLang" class="ms-host"></span>
        <p class="note" data-i18n="settings.language.reload">Changing the language reloads the page.</p>
      </div>

      <div class="ovl-section">
        <h3>${esc(t('settings.autodl.title'))}</h3>
        <label class="chk"><input type="checkbox" id="setAutoDl"> <span
          id="setAutoDlLabel">${esc(t('common.reading'))}</span></label>
        <p class="note">${esc(t('settings.autodl.scope'))}${hint(t('settings.autodl.tip'))}</p>
        <div id="setAutoDlSource"></div>
      </div>

      <div class="ovl-section">
        <h3>${esc(t('settings.safe.title'))}</h3>
        <label class="chk"><input type="checkbox" id="setSafe"> ${
          esc(t('settings.safe.label'))}</label>
        <p class="note">${esc(t('settings.safe.scope.pre'))}<b>${
          esc(t('settings.safe.scope.b'))}</b>${esc(t('settings.safe.scope.post'))}<br>
          ${esc(t('settings.safe.count'))}<span id="setR18">${
          esc(t('common.calculating'))}</span></p>
      </div>

      <div class="ovl-section">
        <h3>${esc(t('settings.heal.title'))}</h3>
        <p class="note">${esc(t('settings.heal.warn'))}<br>
          <b>${esc(t('settings.heal.warn.b'))}</b>${hint(t('settings.heal.tip'))}</p>
        <div id="setHeals">${esc(t('common.reading'))}</div>
      </div>

      <div class="ovl-section">
        <h3>${esc(t('settings.readonly.title'))}</h3>
        <div id="setReadonly">${esc(t('common.reading'))}</div>
      </div>`,
    onMount: (body) => {
      const dl = body.querySelector('#setAutoDl');
      const safe = body.querySelector('#setSafe');

      const paintAuto = (on) => {
        dl.checked = !!on;
        body.querySelector('#setAutoDlLabel').textContent =
          t(on ? 'settings.autodl.on' : 'settings.autodl.off');
      };

      // ⚠️ 換語言**整頁重載**。重繪比讓每個 view 各自重新渲染可靠得多，
      // 而換語言是極低頻動作 —— 為它維護一條「所有畫面都要能重畫」的路徑
      // 不划算，而且那條路徑平常沒人走，壞了也不會有人發現。
      singleDrop(body.querySelector('#setLang'), {
        label: 'Language',
        ariaLabel: 'Language',
        values: LANGS.map((l) => ({ value: l, text: LANG_NAMES[l] })),
        value: currentLang(),
        onChange: async (l) => {
          if (l === currentLang()) return;
          await patchSetting({ language: l });
          location.reload();
        },
      });

      const paintSource = (s) => {
        body.querySelector('#setAutoDlSource').innerHTML =
          sourceLine(s, 'auto_download', onOff);
      };

      /** 偏好檔讀不到。**不是 console.error 了事** —— 這是使用者會來找答案的地方。 */
      const paintPrefsError = (s) => {
        const box = body.querySelector('#setPrefsErr');
        box.classList.toggle('hidden', !s?.prefs_error);
        if (s?.prefs_error) {
          box.innerHTML = `<p class="note bad">${esc(t('settings.prefs.broken'))}${
            hint(t('settings.prefs.broken.tip', { err: s.prefs_error }))}</p>`;
        }
      };

      // 「改用 config.toml 的值」。委派 —— 那顆按鈕是重畫出來的。
      body.addEventListener('click', async (ev) => {
        const btn = ev.target.closest('[data-reset]');
        if (!btn) return;
        btn.disabled = true;
        try {
          const s = await resetSetting(btn.dataset.reset);
          paintAuto(s.auto_download);
          paintSource({ ...state.settings, ...s });
        } catch (e) {
          btn.disabled = false;
          body.querySelector('#setAutoDlLabel').textContent =
            t('settings.reset.failed', { msg: e.message });
        }
      });
      const paintSafe = () => { safe.checked = safeMode(); };
      paintSafe();
      // header 的開關與這裡是**同一個狀態的兩個控制項**，必須即時同步
      onSafeModeChange(paintSafe);
      safe.addEventListener('change', () => setSafeMode(safe.checked));

      dl.addEventListener('change', async () => {
        const want = dl.checked;
        try {
          const s = await setAutoDownload(want);
          paintAuto(s.auto_download);
          paintSource({ ...state.settings, ...s });
        } catch (e) {
          paintAuto(!want);   // 沒切成功就別讓畫面顯示已切換
          body.querySelector('#setAutoDlLabel').textContent =
            t('settings.toggle.failed', { msg: e.message });
        }
      });

      // 設定值與 r18 筆數各自載入 —— r18 那個是一次 COUNT（正式庫約一秒），
      // 不該讓整個面板等它。
      loadSettings().then((s) => {
        paintAuto(s?.auto_download);
        paintSource(s);
        paintPrefsError(s);
        body.querySelector('#setReadonly').innerHTML = readonlyRows(s);
      });
      api('/api/identity/heals?limit=20')
        .then((d) => {
          const box = body.querySelector('#setHeals');
          const pending = `<p class="note">${
            esc(t('settings.heal.pending', { n: fmt.num(d.pending) }))}</p>`;
          box.innerHTML = pending + (d.items.length
            ? `<dl class="kv">${d.items.map((h) => `
                <dt>${esc(h.at.slice(0, 16).replace('T', ' '))}</dt>
                <dd>${esc(h.platform)} @${esc(h.screen_name)} —— ${
                  h.kind === 'merge'
                    ? t('settings.heal.merge', { n: fmt.num(h.moved_posts) })
                    : t('settings.heal.fill')}<br>
                  <span class="muted">${esc(h.placeholder_id)} → ${esc(h.real_id)}</span></dd>`).join('')}</dl>`
            // 0 筆是常態（還沒採集過），要看起來像正常而不是壞掉
            : `<p class="note">${esc(t('settings.heal.none'))}</p>`);
        })
        .catch((e) => {
          body.querySelector('#setHeals').textContent =
            t('common.unreadable', { msg: e.message });
        });

      api('/api/media/count?rating=r18')
        .then((d) => {
          body.querySelector('#setR18').textContent =
            t('common.n.items', { n: fmt.num(d.total) });
        })
        .catch((e) => {
          body.querySelector('#setR18').textContent =
            t('common.uncomputable', { msg: e.message });
        });
    },
  });
  // 已經載過就先用手上的值畫，避免面板一開是空的
  if (state.settings) {
    handle.body.querySelector('#setReadonly').innerHTML = readonlyRows(state.settings);
  }
  return handle;
}
