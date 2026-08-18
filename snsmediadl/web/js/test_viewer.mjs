// viewer.js 的**點擊關閉判斷**。
// 用法: node snsmediadl/web/js/test_viewer.mjs
//
// 為什麼需要這支：這裡釘住的 bug 是「放大檢視時拖曳平移，放開滑鼠就整個關掉」。
// 根因不在拖曳的數學，而在事件目標 —— 平移時我們對 stage 呼叫
// `setPointerCapture`，之後所有 pointer 事件（含合成出來的 `click`）都被
// 重新指向 stage，於是「點背景（target === stage）就關閉」那條分支被誤觸。
//
// 這種事在瀏覽器外看不見（假 DOM 不會模擬 pointer capture 的重新指向），
// 所以把判斷抽成純函式，在這裡用「pointerdown 的目標」而不是 click 的目標
// 直接驗語意：拖過就不關、點在影像上不關、只有真的點到背景才關。

import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const { shouldCloseOnClick } = await import(
  pathToFileURL(join(here, 'viewer.js')).href);

let failed = 0;
function check(name, cond, extra = '') {
  if (cond) { console.log(`  ok  ${name}`); return; }
  failed += 1;
  console.log(`  FAIL ${name}${extra ? ` —— ${extra}` : ''}`);
}

// 這三個只當識別用，不需要是真的 DOM 節點。
const root = { name: 'root' };
const stage = { name: 'stage' };
const img = { name: 'img' };

// 1. 就是那個 bug：在影像上拖曳平移，放開滑鼠不可以關掉檢視器。
//    （真實瀏覽器裡此時 click 的 target 已經被 capture 改成 stage。）
check('拖曳後放開不關閉（在影像上起手）',
      shouldCloseOnClick({ downTarget: img, dragged: true, stage, root }) === false);

// 2. 從背景起手的拖曳同樣不關 —— 使用者是在平移，不是在點背景。
check('拖曳後放開不關閉（在背景起手）',
      shouldCloseOnClick({ downTarget: stage, dragged: true, stage, root }) === false);

// 3. 純點擊影像不關閉。這是同一個根因的另一半：capture 之後
//    連沒有位移的點擊都會把 target 變成 stage。
check('點影像不關閉',
      shouldCloseOnClick({ downTarget: img, dragged: false, stage, root }) === false);

// 4. 真的點到背景才關閉（stage 本身、或遮罩最外層）。
check('點 stage 背景關閉',
      shouldCloseOnClick({ downTarget: stage, dragged: false, stage, root }) === true);
check('點遮罩最外層關閉',
      shouldCloseOnClick({ downTarget: root, dragged: false, stage, root }) === true);

// 5. 還沒發生過 pointerdown（例如鍵盤按 Enter 觸發的 click）不該關閉 ——
//    downTarget 是 null，不能因為「不等於任何東西」就掉進關閉分支。
check('沒有 pointerdown 目標時不關閉',
      shouldCloseOnClick({ downTarget: null, dragged: false, stage, root }) === false);

console.log(failed ? `\n${failed} 項失敗` : '\n全部通過');
process.exit(failed ? 1 : 0);
