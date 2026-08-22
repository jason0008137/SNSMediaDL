// ugoira.js 的時間推進。走 testkit，node 與瀏覽器都跑得動。
//
// 只測純函式：`createUgoiraPlayer` 要 canvas / ImageBitmap / rAF，那些在
// node 裡沒有，而**時間推進正是這支模組唯一會算錯的東西** —— 播放器其餘
// 部分是 DOM 接線，看得見壞掉。

import { test, assert, assertEqual, run } from './testkit.js';
import { buildCumulative, frameAt } from './ugoira.js';

// 刻意不等長：等長的表會讓「用固定 fps 除一除」這種錯誤實作也剛好通過，
// 而真的 ugoira 每格延遲可以不同。
const FRAMES = [{ delay: 40 }, { delay: 70 }, { delay: 120 }];
const CUM = buildCumulative(FRAMES);   // [40, 110, 230]

test('累積時間表是每格的結束時刻', () => {
  assertEqual(CUM, [40, 110, 230]);
  assertEqual(buildCumulative([]), []);
});

test('每一格的區間都是左閉右開', () => {
  // 0–39 → 第 0 格；40 是第 1 格的第一毫秒，不是第 0 格的最後一毫秒
  assertEqual(frameAt(CUM, 0), 0);
  assertEqual(frameAt(CUM, 39), 0);
  assertEqual(frameAt(CUM, 40), 1);
  assertEqual(frameAt(CUM, 109), 1);
  assertEqual(frameAt(CUM, 110), 2);
  assertEqual(frameAt(CUM, 229), 2);
});

test('循環：走到底回繞，超過好幾輪也對', () => {
  assertEqual(frameAt(CUM, 230), 0, '整整一輪之後回到第 0 格');
  assertEqual(frameAt(CUM, 270), 1);
  // 分頁切到背景時 rAF 不跑，切回來 elapsed 會一次跳很多輪 ——
  // 自己減一輪的實作在這裡會錯，取餘數才對
  assertEqual(frameAt(CUM, 230 * 100 + 150), 2, '一百輪之後仍然算得對');
});

test('不循環：走到底停在最後一格', () => {
  const opt = { loop: false };
  assertEqual(frameAt(CUM, 229, opt), 2);
  assertEqual(frameAt(CUM, 230, opt), 2, '不是回到第 0 格');
  assertEqual(frameAt(CUM, 99999, opt), 2, '也不是消失');
});

test('落後時跳格，而不是排隊補播', () => {
  // 一次前進 200 ms（例如 GC 停頓）應該直接落在第 2 格，
  // 而不是「先補畫第 1 格」—— 維持總時長正確比每格都畫出來重要
  assertEqual(frameAt(CUM, 200), 2);
});

test('負數與零長度不會炸', () => {
  assertEqual(frameAt(CUM, -50), 0);
  assertEqual(frameAt([], 10), 0);
  // 每格 delay 都是 0 → 總長 0。取餘數會變成 NaN，必須擋掉
  assertEqual(frameAt(buildCumulative([{ delay: 0 }, { delay: 0 }]), 5), 0);
});

test('延遲相同的表也要對（最常見的情況）', () => {
  // 實檔就是這樣：96 格全 40 ms
  const cum = buildCumulative(Array.from({ length: 96 }, () => ({ delay: 40 })));
  assertEqual(cum[95], 3840, '總長 3.84 秒');
  assertEqual(frameAt(cum, 3839), 95);
  assertEqual(frameAt(cum, 3840), 0);
  assert(frameAt(cum, 1000) === 25, '第 1 秒是第 25 格');
});

await run('test_ugoira');
