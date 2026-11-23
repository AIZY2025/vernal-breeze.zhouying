(function () {
  'use strict';

  var SEAT_COLOR = { A1: '#6f8f5f', A2: '#5b7f96', A3: '#a9667a', A4: '#4f8a76', A5: '#b08a4f' };
  var QUICK = [
    { label: 'A 质疑正方', fill: '我质疑正方第一条论据，' },
    { label: 'B 质疑反方', fill: '反方说的' },
    { label: 'C 改参数', fill: '我把可动用存款改成150万' },
{ label: 'D 补背景', fill: '补充一个情况：' },
  { label: 'E 结束出结论', fill: '结束沙盘，给我结论' },
  { label: 'F 停止推演', fill: '停止推演' }
];

  var stream = document.getElementById('stream');
  var composer = document.getElementById('composer');
  var input = document.getElementById('input');
  var hint = document.getElementById('composer-hint');
  var sendBtn = document.getElementById('send');
  var sendLabel = sendBtn.querySelector('span');
  var quick = document.getElementById('quick');
  var modePill = document.getElementById('mode-pill');
  var runPill = document.getElementById('run-pill');
  var resetBtn = document.getElementById('reset-btn');
  var stopBtn = document.getElementById('stop-btn');
  var providerSel = document.getElementById('provider');
  var skipBtn = document.getElementById('skip-btn');
  var historyBtn = document.getElementById('history-btn');
  var historyBox = document.getElementById('history');
  var historyList = document.getElementById('history-list');
  var historyClose = document.getElementById('history-close');
  var phaseNodes = Array.prototype.slice.call(document.querySelectorAll('#phases li'));
  var seatNodes = {};

  Array.prototype.forEach.call(document.querySelectorAll('#seats li'), function (li) {
    seatNodes[li.dataset.seat] = li;
    li.style.setProperty('--seat-color', SEAT_COLOR[li.dataset.seat]);
  });

  var lastId = 0;
  var awaiting = false;
  var stage = 'idle';
  var phase = 0;
  var currentPrompt = '';
  var es = null;
  var waitTimer = null;
  var waitStart = 0;
  var waitText = '智能体正在处理…';
  var retry = 0;
  var retryTimer = null;
  var renderQueue = [];
  var pumping = false;
  var paused = false;
  var fastMode = false;
  var parallelBox = null;
  var seatStart = {};
  var seatTimer = null;
  var connectAt = 0;
  var replayWatch = false;
  var bootCount = 0;
  var bootHistory = 0;
  var SEAT_NAME = {
    A1: 'A1 背景探询', A2: 'A2 支持方', A3: 'A3 反对方', A4: 'A4 量化测算', A5: 'A5 仲裁汇总'
  };
  var REDUCE_MOTION = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  if (REDUCE_MOTION) {
    fastMode = true;
    document.documentElement.dataset.pace = 'fast';
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function nearBottom() {
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 180;
  }

  function append(node) {
    var stick = nearBottom();
    stream.appendChild(node);
    if (stick) stream.scrollTop = stream.scrollHeight;
  }

  function shortPath(path) {
    if (!path) return '';
    var parts = String(path).split(/[\\/]/).filter(Boolean);
    return parts[parts.length - 1] || path;
  }

  function downloadText(text, name) {
    var blob = new Blob([String(text || '')], { type: 'text/plain;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = String(name || '沙盘记录.txt').replace(/[\\/:*?"<>|]/g, '_');
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function downloadButton(text, name) {
    var wrap = el('div', 'msg-actions');
    var btn = el('button', 'mini', '下载');
    btn.type = 'button';
    btn.addEventListener('click', function (event) {
      event.stopPropagation();
      downloadText(text, name);
    });
    wrap.appendChild(btn);
    return wrap;
  }

  function downloadJson(obj, name) {
    var blob = new Blob([JSON.stringify(obj, null, 2)],
      { type: 'application/json;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = String(name || '沙盘导出.json').replace(/[\\/:*?"<>|]/g, '_');
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function addSystem(text, cls) {
    append(el('div', cls || 'sys', text));
  }

  function setSeat(key, state) {
    var node = seatNodes[key];
    if (!node) return;
    node.classList.toggle('is-running', state === 'running');
    node.classList.toggle('is-done', state === 'done');
    if (state === 'running') {
      seatStart[key] = Date.now();
      node.querySelector('.seat-state').textContent = '研判中 0s';
    } else {
      delete seatStart[key];
      node.querySelector('.seat-state').textContent = state === 'done' ? '完成' : '待命';
    }
    if (Object.keys(seatStart).length) {
      if (!seatTimer) seatTimer = setInterval(tickSeats, 1000);
    } else if (seatTimer) {
      clearInterval(seatTimer);
      seatTimer = null;
    }
  }

  function tickSeats() {
    Object.keys(seatStart).forEach(function (key) {
      var node = seatNodes[key];
      if (!node) return;
      var sec = Math.floor((Date.now() - seatStart[key]) / 1000);
      node.querySelector('.seat-state').textContent = '研判中 ' + sec + 's';
      if (parallelBox) {
        var card = parallelBox.querySelector('[data-seat="' + key + '"]');
        var state = card && card.querySelector('.parallel-state');
        if (state && card.classList.contains('is-pending')) {
          state.textContent = '独立研判中 ' + sec + 's';
        }
      }
    });
    if (!Object.keys(seatStart).length && seatTimer) {
      clearInterval(seatTimer);
      seatTimer = null;
    }
  }

  function resetSeats() {
    Object.keys(seatNodes).forEach(function (key) {
      setSeat(key, 'idle');
    });
  }

  var PHASE_WIND = [0.2, 0.3, 0.45, 0.9, 0.3, 0.5, 0.1];
  var PHASE_NAME = ['idle', 'gate', 'probe', 'parallel', 'board', 'interact', 'final'];
  var baseWind = 0.2;

  function applyWind(value) {
    document.documentElement.style.setProperty('--wind', String(value));
  }

  function scheduleGust() {
    setTimeout(function () {
      if (REDUCE_MOTION || fastMode || document.hidden) {
        scheduleGust();
        return;
      }
      applyWind(Math.min(1, baseWind + 0.3));
      document.body.classList.add('is-gusting');
      setTimeout(function () {
        applyWind(baseWind);
        document.body.classList.remove('is-gusting');
        scheduleGust();
      }, 1600 + Math.random() * 1200);
    }, 10000 + Math.random() * 8000);
  }

  function setPhase(n) {
    phase = n;
    phaseNodes.forEach(function (li, i) {
      li.classList.toggle('is-done', i + 1 < n);
      li.classList.toggle('is-current', i + 1 === n);
    });
    var idx = Math.max(0, Math.min(6, n));
    baseWind = PHASE_WIND[idx];
    applyWind(baseWind);
    document.body.dataset.phase = PHASE_NAME[idx];
  }

  function setModePill(text, active) {
    if (text) modePill.textContent = active ? '运行中 · ' + text : text;
    modePill.classList.toggle('pill--live', !!active);
    modePill.title = text || '';
  }

  function syncComposer() {
    var canType = stage === 'idle' || (stage === 'running' && awaiting);
    input.disabled = !canType;
    sendBtn.disabled = !canType;
    composer.classList.toggle('is-waiting', canType && awaiting);
    sendLabel.textContent = stage === 'idle' ? '开始' : '发送';
    var showQuick = canType && awaiting && currentPrompt.indexOf('指令') >= 0;
    quick.hidden = !showQuick;
    stopBtn.hidden = !(stage === 'running' || stage === 'stopping');
    stopBtn.disabled = stage === 'stopping';
    if (canType && awaiting && document.activeElement !== input) input.focus();
  }

  function stopWaitHint() {
    if (waitTimer) {
      clearInterval(waitTimer);
      waitTimer = null;
    }
  }

  function startWaitHint(text) {
    stopWaitHint();
    waitText = text || '智能体正在处理…';
    waitStart = Date.now();
    hint.textContent = waitText;
    waitTimer = setInterval(function () {
      var sec = Math.floor((Date.now() - waitStart) / 1000);
      hint.textContent = waitText + ' ' + sec + 's' + (sec >= 20 ? '（模型仍在生成，请稍候）' : '');
    }, 1000);
  }

  function setAwaiting(flag, prompt) {
    awaiting = flag;
    currentPrompt = prompt || '';
    if (flag) {
      stopWaitHint();
      hint.textContent = prompt.indexOf('指令') >= 0
        ? '可发指令：A 质疑正方 / B 质疑反方 / C 改参数 / D 补背景 / E 结束'
        : '回答上面这个问题，信息越具体，档案越准。';
    }
    syncComposer();
  }

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 168) + 'px';
  }

  function clearInput() {
    input.value = '';
    autoGrow();
  }

  function pushUser(text) {
    var wrap = el('div', 'msg msg--user');
    wrap.appendChild(el('div', 'bubble', text));
    append(wrap);
  }

  function seatOf(name) {
    var m = String(name || '').match(/A[1-5]/);
    return m ? m[0] : '';
  }

  function chunkText(text) {
    var rest = String(text || '');
    var out = [];
    while (rest.length > 72) {
      var cut = 64;
      for (var i = 72; i > 40; i -= 1) {
        if ('。；\n！？'.indexOf(rest.charAt(i)) >= 0) { cut = i + 1; break; }
      }
      out.push(rest.slice(0, cut));
      rest = rest.slice(cut);
    }
    if (rest) out.push(rest);
    return out;
  }

  function revealText(node, text, fast) {
    node.textContent = '';
    if (fast) { node.textContent = text; return; }
    var chunks = chunkText(text);
    var index = 0;
    (function step() {
      if (index >= chunks.length) return;
      node.textContent += chunks[index];
      index += 1;
      setTimeout(step, 150);
    })();
  }

  function agentCard(key, name) {
    var card = el('article', 'msg msg--agent');
    card.style.setProperty('--seat-color', SEAT_COLOR[key] || '#6f8f5f');
    var head = el('header', 'msg-head');
    head.appendChild(el('span', 'avatar', key || 'AG'));
    head.appendChild(el('span', 'who', name || '智能体'));
    card.appendChild(head);
    card.appendChild(el('div', 'msg-body', ''));
    return card;
  }

  function openParallel() {
    if (parallelBox) return;
    parallelBox = el('section', 'parallel');
    ['A2', 'A3', 'A4'].forEach(function (key) {
      var card = agentCard(key, SEAT_NAME[key]);
      card.dataset.seat = key;
      card.classList.add('is-pending');
      card.querySelector('.msg-head').appendChild(el('span', 'parallel-state', '独立研判中 0s'));
      parallelBox.appendChild(card);
      setSeat(key, 'running');
    });
    append(parallelBox);
  }

  function closeParallel() {
    if (!parallelBox) return;
    Array.prototype.forEach.call(parallelBox.querySelectorAll('.is-pending'), function (card) {
      var state = card.querySelector('.parallel-state');
      if (state) state.textContent = '未返回结果';
    });
    parallelBox = null;
  }

  function renderAgent(ev, fast) {
    var key = seatOf(ev.name);
    var isIntel = String(ev.name || '').indexOf('外部情报') >= 0;
    var target = null;
    if (parallelBox && (key === 'A2' || key === 'A3' || key === 'A4')) {
      target = parallelBox.querySelector('[data-seat="' + key + '"]');
    }
    var card = target || agentCard(key, ev.name || '智能体');
    if (isIntel && !target) card.classList.add('msg--intel');
    var head = card.querySelector('.msg-head');
    var tag = head.querySelector('.tag');
    if (!tag) {
      tag = el('span', 'tag');
      head.appendChild(tag);
    }
    tag.textContent = ev.tag || '';
    if (target) {
      target.classList.remove('is-pending');
      var state = target.querySelector('.parallel-state');
      if (state) state.textContent = ev.tag || '完成';
    }
    revealText(card.querySelector('.msg-body'), ev.text || '', fast === true || fastMode);
    if (!target) append(card);
    if (key) setSeat(key, key === 'A1' ? 'running' : 'done');
  }

  function enqueue(ev) {
    renderQueue.push(ev);
    pumpQueue();
  }

  function pumpQueue() {
    if (pumping || paused || !renderQueue.length) return;
    pumping = true;
    var ev = renderQueue.shift();
    var wait = paint(ev, fastMode);
    setTimeout(function () {
      pumping = false;
      pumpQueue();
    }, fastMode ? 0 : wait);
  }

  function paint(ev, fast) {
    if (ev.type === 'agent') {
      renderAgent(ev, fast);
      return 160;
    }
    if (ev.type === 'info') {
      handleInfo(ev);
      return fast ? 0 : 220;
    }
    if (ev.type === 'show') {
      renderShow(ev);
      return fast ? 0 : 260;
    }
    if (ev.type === 'ask') {
      renderAsk(ev);
      paused = true;
      return 0;
    }
    return 0;
  }

  function flushQueue() {
    while (renderQueue.length) paint(renderQueue.shift(), true);
  }

  function setFast(on) {
    if (!on || fastMode) return;
    fastMode = true;
    document.documentElement.dataset.pace = 'fast';
    document.body.classList.remove('is-gusting');
    flushQueue();
    if (typeof skipBtn !== 'undefined' && skipBtn) skipBtn.hidden = true;
  }

  function boardTitle(text) {
    var lines = String(text || '').split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (line && !/^[\s\u2500\u2014\u2013\-_=]+$/.test(line)) return line;
    }
    return '沙盘看板';
  }

  function splitVerdict(text) {
    var lines = String(text || '').split('\n');
    var verdict = '';
    var body = [];
    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (!verdict && trimmed.indexOf('【仲裁判定】') === 0) {
        verdict = trimmed.replace('【仲裁判定】', '').trim();
      } else {
        body.push(line);
      }
    });
    return { verdict: verdict, body: body.join('\n') };
  }

  function renderShow(ev) {
    var text = ev.text || '';
    var isReport = ev.final === true || text.indexOf('_SESSION=CLOSED') >= 0;
    var isArchive = text.indexOf('《决策背景档案》') >= 0;
    var card = el('article', 'msg msg--board' + (isReport ? ' msg--report' : ''));
    var head = el('header', 'msg-head');
    head.appendChild(el('span', 'who', boardTitle(text)));
    head.appendChild(el('span', 'tag', isReport ? '收尾 · A5' : 'A5 仲裁汇总'));
    head.appendChild(downloadButton(text, boardTitle(text) + '.txt'));
    card.appendChild(head);
    var parts = splitVerdict(text);
    if (parts.verdict) {
      var verdict = el('div', 'verdict');
      verdict.appendChild(el('span', 'verdict-label', '仲裁判定'));
      verdict.appendChild(el('span', 'verdict-text', parts.verdict));
      card.appendChild(verdict);
    }
    card.appendChild(el('div', 'msg-body', parts.body));
    append(card);
    setSeat('A5', 'done');
    if (isArchive) setSeat('A1', 'done');
    if (isReport) {
      stage = 'done';
      awaiting = false;
      syncComposer();
      dropFinalPetal();
    }
    if (!isArchive && stage !== 'done' && phase < 4) setPhase(4);
  }

  function renderAsk(ev) {
    var prompt = ev.prompt || '';
    var silent = ev.echo === false;
    if (!silent) {
      var body = prompt.indexOf('指令') >= 0
        ? '看板已就绪。现在可以质疑、改参数或补背景，说人话就行。'
        : prompt;
      var card = el('article', 'msg msg--ask');
      card.appendChild(el('span', 'ask-label', '待你回答'));
      card.appendChild(el('div', 'msg-body', body));
      append(card);
    }
    setAwaiting(true, prompt);
    setPhase(silent || prompt.indexOf('指令') < 0 ? 2 : 5);
  }

  function addError(text, extra) {
    var card = el('article', 'msg msg--error');
    card.style.setProperty('--seat-color', '#a8462f');
    var head = el('header', 'msg-head');
    head.appendChild(el('span', 'avatar', '!'));
    head.appendChild(el('span', 'who', '链路提示'));
    card.appendChild(head);
    card.appendChild(el('div', 'msg-body', text || '发生未知错误'));
    if (extra) card.appendChild(el('div', 'hint', extra));
    append(card);
    stage = 'done';
    awaiting = false;
    stopWaitHint();
    hint.textContent = '本场已中止。点击「新推演」重新开始。';
    syncComposer();
  }

  function handleInfo(ev) {
    var text = ev.text || '';
    if (text.indexOf('阶段 3.5/6') >= 0) closeParallel();
    var matched = text.match(/阶段\s*(\d)\s*\/\s*6/);
    if (matched) {
      var n = Number(matched[1]);
      if (n === 3) openParallel();
      setPhase(n);
    }
    if (text.indexOf('_BACKGROUND_READY') >= 0) setSeat('A1', 'done');
    var started = text.match(/^(A[1-5])\s*已启动/);
    if (started) setSeat(started[1], 'running');
    var route = text.indexOf('路由判定') >= 0 || text.indexOf('参数已更新') >= 0;
    addSystem(text, route ? 'sys sys--route' : 'sys');
  }

  function runLabel(ev) {
    return ev.run_id || shortPath(ev.run_dir);
  }

  function handleDone(ev) {
    stage = 'done';
    awaiting = false;
    stopWaitHint();
    if (ev.stopped) {
      setModePill(null, false);
      addSystem('已停止推演 ｜ 未生成最终报告，档案与已生成的看板保留：' + runLabel(ev),
        'sys sys--done');
      hint.textContent = '已停止。点「新推演」开新的一场，或到「历史」查看本场记录。';
      syncComposer();
      return;
    }
    if (ev.refused) {
      addSystem('未进入推演 ｜ ' + (ev.reason || ''), 'sys sys--route');
      setPhase(1);
      setModePill(null, false);
      hint.textContent = '只处理「要不要 / 该不该 / 选哪个」这类个人决策，换个问法再试。';
      syncComposer();
      return;
    }
    setPhase(6);
    setModePill(null, false);
    addSystem('本场推演结束 ｜ 全轮次记录已保存：' + runLabel(ev), 'sys sys--done');
    hint.textContent = '已结束。点击「新推演」开新的一场。';
    syncComposer();
  }

  function applyStatus(ev) {
    if (ev.mode) setModePill(ev.mode, ev.active);
    if (ev.run_dir || ev.run_id) {
      runPill.hidden = false;
      runPill.textContent = '记录 · ' + runLabel(ev);
      runPill.title = ev.run_id || '';
    }
    if (ev.active && stage === 'idle') stage = 'running';
    if (!ev.active && stage === 'stopping') {
      stage = 'done';
      hint.textContent = '已停止。点「新推演」开新的一场。';
      syncComposer();
    } else if (!ev.active && stage === 'running' && !awaiting) syncComposer();
  }

  function handle(ev) {
    switch (ev.type) {
      case 'agent':
      case 'info':
      case 'show':
        enqueue(ev);
        break;
      case 'user':
        pushUser(ev.text || '');
        awaiting = false;
        syncComposer();
        break;
      case 'ask':
        enqueue(ev);
        break;
      case 'status':
        applyStatus(ev);
        break;
      case 'stats':
        if (ev.calls) {
          addSystem('模型调用 ' + ev.calls + ' 次 ｜ 累计 ' + ev.seconds + ' 秒', 'sys');
        }
        break;
      case 'done':
        flushQueue();
        paused = false;
        handleDone(ev);
        break;
      case 'error':
        flushQueue();
        paused = false;
        addError(ev.text, ev.hint || ev.detail);
        break;
      default: break;
    }
  }

  function connect(since) {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (es) {
      es.close();
      es = null;
    }
    lastId = since || 0;
    replayWatch = lastId === 0 && bootHistory > 3;
    connectAt = Date.now();
    bootCount = 0;
    es = new EventSource('/api/stream?since=' + lastId);
    es.onopen = function () {
      retry = 0;
    };
    es.onmessage = function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (typeof data.id === 'number') lastId = data.id;
      if (replayWatch) {
        bootCount += 1;
        if (bootCount > 2 && Date.now() - connectAt < 600) setFast(true);
      }
      handle(data);
    };
    es.onerror = function () {
      if (!es) return;
      es.close();
      es = null;
      retry = Math.min(retry + 1, 6);
      var delay = Math.min(30000, 1000 * Math.pow(2, retry - 1));
      retryTimer = setTimeout(function () {
        retryTimer = null;
        connect(lastId);
      }, delay);
    };
  }

  function renderWelcome() {
    var box = el('section', 'welcome');
    box.appendChild(el('div', 'eyebrow', '五智能体 · 六阶段 · 本地推演'));
    box.appendChild(el('h2', '', '一句话说出你在纠结的决策，五个 Agent 替你吵一架。'));
    box.appendChild(el('p', '', 'A1 先摸清背景；A2 / A3 / A4 互不可见地各自研判；A5 汇总成看板。之后你可以质疑任意一方、改数字、补背景，每次介入都会生成新版本。'));
    box.appendChild(el('p', '', '全程记录落在 runs/ 目录，质疑与修订都会留痕。'));

    var chips = el('div', 'chips');
    ['我要不要买一台二十万级的家用车', '我要不要接受这份异地的工作机会', '我要不要辞职做独立开发'].forEach(function (text) {
      var chip = el('button', 'chip', text);
      chip.type = 'button';
      chip.addEventListener('click', function () {
        input.value = text;
        input.focus();
        autoGrow();
      });
      chips.appendChild(chip);
    });
    var paste = el('button', 'chip', '粘贴已有档案（跳过 A1）');
    paste.type = 'button';
    paste.addEventListener('click', function () {
      if (stage !== 'idle') return;
      var raw = window.prompt('粘贴《决策背景档案》JSON（至少包含 question 与 finance）：');
      if (!raw) return;
      var archive;
      try {
        archive = JSON.parse(raw);
      } catch (err) {
        addSystem('JSON 解析失败：' + err.message, 'sys sys--error');
        return;
      }
      if (!archive || typeof archive !== 'object' || Array.isArray(archive)) {
        addSystem('档案必须是 JSON 对象。', 'sys sys--error');
        return;
      }
      startWithArchive(archive);
    });
    chips.appendChild(paste);
    box.appendChild(chips);
    append(box);
  }

  function clearBoard() {
    historyBox.hidden = true;
    stream.innerHTML = '';
    renderQueue = [];
    pumping = false;
    paused = false;
    fastMode = REDUCE_MOTION;
    parallelBox = null;
    seatStart = {};
    if (seatTimer) {
      clearInterval(seatTimer);
      seatTimer = null;
    }
    document.documentElement.dataset.pace = fastMode ? 'fast' : '';
    hint.textContent = '先说清你在纠结什么，例如：我要不要买一台二十万级的家用车';
    input.placeholder = '先说清你在纠结什么…';
    stage = 'idle';
    awaiting = false;
    currentPrompt = '';
    resetSeats();
    setPhase(0);
    setModePill('未开始', false);
    runPill.hidden = true;
    clearInput();
    syncComposer();
    renderWelcome();
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || ('服务返回 ' + res.status));
        return data;
      });
    });
  }

  function startWithArchive(archive) {
    if (stage !== 'idle') return;
    var query = String(archive.question || '').trim();
    if (!query) {
      addSystem('档案缺少 question 字段，无法开始。', 'sys sys--error');
      return;
    }
    stage = 'running';
    setPhase(1);
    setSeat('A1', 'done');
    setAwaiting(false);
    startWaitHint('正在载入档案并唤起三方研判…');
    post('/api/start', {
      query: query,
      provider: providerSel ? providerSel.value : '',
      archive: archive
    }).then(function () {
      var welcome = stream.querySelector('.welcome');
      if (welcome) welcome.remove();
      clearInput();
      connect(lastId);
    }).catch(function (err) {
      addError(err.message);
    });
  }

  function submit() {
    var text = input.value.trim();
    if (!text) return;
    if (stage === 'idle') {
      stage = 'running';
      setPhase(1);
      setSeat('A1', 'running');
      setAwaiting(false);
      startWaitHint('正在唤起 A1 背景探询…');
      post('/api/start', { query: text, provider: providerSel ? providerSel.value : '' }).then(function () {
        var welcome = stream.querySelector('.welcome');
        if (welcome) welcome.remove();
        clearInput();
        connect(lastId);
      }).catch(function (err) {
        addError(err.message);
      });
      return;
    }
    if (stage === 'running' && awaiting) {
      var prompt = currentPrompt;
      setAwaiting(false, prompt);
      startWaitHint('智能体正在处理…');
      post('/api/answer', { text: text }).then(function () {
        clearInput();
        paused = false;
        pumpQueue();
      }).catch(function (err) {
        hint.textContent = err.message;
        stopWaitHint();
        setAwaiting(true, prompt);
      });
    }
  }

  composer.addEventListener('submit', function (event) {
    event.preventDefault();
    submit();
  });

  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  QUICK.forEach(function (item) {
    var btn = el('button', '', item.label);
    btn.type = 'button';
    btn.addEventListener('click', function () {
      if (input.disabled) return;
      input.value = item.fill;
      input.focus();
      input.setSelectionRange(item.fill.length, item.fill.length);
      autoGrow();
    });
    quick.appendChild(btn);
  });

  stopBtn.addEventListener('click', function () {
    if (stage !== 'running') return;
    var prevAwaiting = awaiting;
    var prevPrompt = currentPrompt;
    stage = 'stopping';
    awaiting = false;
    stopWaitHint();
    flushQueue();
    paused = false;
    setFast(true);
    hint.textContent = '正在停止推演，等待当前这一步收尾…';
    syncComposer();
    addSystem('已发出停止请求：后续阶段不再推进，也不生成最终报告。', 'sys sys--route');
    post('/api/stop', {}).then(function (data) {
      if (!data || !data.stopped) {
        if (stage === 'stopping') {
          stage = 'done';
          hint.textContent = '当前没有进行中的推演。';
          syncComposer();
        }
        return;
      }
      if (stage === 'stopping') hint.textContent = '正在停止推演，等待当前这一步收尾…';
    }).catch(function (err) {
      stage = 'running';
      setAwaiting(prevAwaiting, prevPrompt);
      var stale = /not found|404/i.test(err.message || '');
      hint.textContent = stale
        ? '停止接口未生效：服务端还是旧版本，重启 python run_web.py 后再试。'
        : '停止请求失败：' + err.message + '。可以再点一次。';
      syncComposer();
    });
  });

  resetBtn.addEventListener('click', function () {
    if (stage === 'running' || stage === 'stopping') {
      addSystem('推演进行中：先点「停止推演」，等后台收尾后即可开新场。', 'sys sys--error');
      return;
    }
    post('/api/reset', {}).then(function () {
      clearBoard();
      connect(0);
    }).catch(function (err) {
      addError(err.message);
    });
  });

  fetch('/api/status').then(function (res) { return res.json(); }).then(function (status) {
    bootHistory = Number(status.history || 0);
    if (!status.history) renderWelcome();
    if (status.active) {
      stage = 'running';
      setModePill(status.mode || '', true);
    } else if (status.mode) {
      setModePill(status.mode, false);
    }
    if (status.run_dir || status.run_id) {
      runPill.hidden = false;
      runPill.textContent = '记录 · ' + runLabel(status);
      runPill.title = status.run_id || '';
    }
    syncComposer();
    checkReviews();
    connect(0);
  }).catch(function () {
    renderWelcome();
    syncComposer();
    connect(0);
  });

  function checkReviews() {
    fetch('/api/reviews').then(function (res) { return res.json(); }).then(function (data) {
      var reviews = (data && data.reviews) || [];
      var due = reviews.filter(function (item) { return item.overdue; });
      if (!due.length) return;
      addSystem('有 ' + due.length + ' 场决策到了复查时间：'
        + due.slice(0, 3).map(function (item) { return item.query || item.id; }).join('；')
        + '。可在「历史」里打开复查清单。', 'sys sys--route');
    }).catch(function () {});
  }

  function renderRunRow(item) {
    var row = el('button', 'run-row');
    row.type = 'button';
    row.appendChild(el('span', 'run-title', item.query || '（未记录问题）'));
    var meta = (item.started_at || item.id) + ' ｜ ' + (item.version || '—') + ' ｜ ' + (item.status || '');
    if (item.resumable) meta += ' ｜ 可续跑';
    row.appendChild(el('span', 'run-meta', meta));
    row.addEventListener('click', function () { openRun(item); });
    return row;
  }

  function openHistory() {
    historyBox.hidden = false;
    historyList.innerHTML = '';
    historyList.appendChild(el('p', 'drawer-note', '正在读取…'));
    fetch('/api/runs').then(function (res) { return res.json(); }).then(function (data) {
      historyList.innerHTML = '';
      var runs = (data && data.runs) || [];
      if (!runs.length) {
        historyList.appendChild(el('p', 'drawer-note', '还没有历史场次。'));
        return;
      }
      runs.forEach(function (item) { historyList.appendChild(renderRunRow(item)); });
    }).catch(function () {
      historyList.innerHTML = '';
      historyList.appendChild(el('p', 'drawer-note', '读取失败，请稍后再试。'));
    });
  }

  function validRunId(id) {
    return /^\d{8}-\d{6}(?:-\d+)?$/.test(String(id || ''));
  }

  function runKindLabel(kind) {
    return kind === 'report' ? '最终报告' : (kind === 'board' ? '看板' : '全轮次记录');
  }

  function intelFacts(item) {
    var facts = (item && item.facts) || {};
    var bits = [];
    (facts.amounts || []).slice(0, 2).forEach(function (entry) {
      if (entry && entry.raw) bits.push(entry.raw);
    });
    (facts.rates || []).slice(0, 2).forEach(function (entry) {
      if (entry && entry.raw) bits.push(entry.raw);
    });
    (facts.keywords || []).slice(0, 2).forEach(function (word) {
      if (word) bits.push(word);
    });
    return bits;
  }

  function renderIntelPanel(data) {
    var intel = (data && data.intel) || {};
    var items = intel.items || [];
    if (!items.length) return null;
    var panel = el('section', 'intel-panel');
    var head = el('div', 'intel-head');
    head.appendChild(el('strong', '', '外部情报 · ' + (intel.provider || '搜索源')));
    head.appendChild(el('span', 'intel-meta',
      items.length + ' 条 ｜ 抓取于 ' + (intel.retrieved_at || '未记录')));
    panel.appendChild(head);
    if (data.usage) panel.appendChild(el('pre', 'intel-usage', data.usage));
    items.forEach(function (item) {
      var row = el('div', 'intel-item');
      var link = el('a', 'intel-title', item.title || '未命名条目');
      link.href = item.url || '#';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      row.appendChild(link);
      var meta = (item.site || '来源') + ' ｜ ' + (item.date || '日期未记录');
      if (item.origin) meta += ' ｜ 发起 ' + item.origin;
      row.appendChild(el('div', 'intel-meta', meta));
      if (item.request) row.appendChild(el('div', 'intel-facts', '用途：' + item.request));
      var bits = intelFacts(item);
      if (bits.length) row.appendChild(el('div', 'intel-facts', '要点：' + bits.join('、')));
      if (item.excerpt) row.appendChild(el('p', 'intel-excerpt', item.excerpt));
      if (item.page_status) row.appendChild(el('div', 'intel-meta', item.page_status));
      panel.appendChild(row);
    });
    return panel;
  }

  function openRun(item) {
    if (stage === 'running' || stage === 'stopping') {
      hint.textContent = '推演进行中，结束后再看历史。';
      return;
    }
    if (!validRunId(item.id)) {
      addSystem('场次编号不合法，已忽略。', 'sys sys--error');
      return;
    }
    historyList.innerHTML = '';
    historyList.appendChild(el('p', 'drawer-note', '正在读取…'));
    var base = '/api/runs/' + encodeURIComponent(item.id);
    var filesRequest = fetch(base + '/files').then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || ('读取失败 ' + res.status));
        return data;
      });
    });
    var intelRequest = fetch(base + '/intel').then(function (res) {
      return res.ok ? res.json() : null;
    }).catch(function () { return null; });
    Promise.all([filesRequest, intelRequest]).then(function (results) {
      var data = results[0];
      var intelData = results[1];
      historyList.innerHTML = '';
      var back = el('button', 'run-row');
      back.type = 'button';
      back.appendChild(el('span', 'run-title', '← 返回场次列表'));
      back.addEventListener('click', openHistory);
      historyList.appendChild(back);
      var panel = renderIntelPanel(intelData);
      if (panel) historyList.appendChild(panel);
      var files = (data && data.files) || [];
      if (!files.length) historyList.appendChild(el('p', 'drawer-note', '这场没有可读文件。'));
      files.forEach(function (file) {
        var row = el('button', 'run-row');
        row.type = 'button';
        row.appendChild(el('span', 'run-title', file.label || file.kind));
        row.appendChild(el('span', 'run-meta', file.kind));
        row.addEventListener('click', function () { loadRunFile(item, file); });
        historyList.appendChild(row);
      });
      if (item.resumable) {
        var resume = el('button', 'run-row run-row--resume');
        resume.type = 'button';
        resume.appendChild(el('span', 'run-title', '继续这场推演'));
        resume.appendChild(el('span', 'run-meta',
          '从 ' + (item.version || '最近版本') + ' 继续，可再质疑 / 改参数 / 补背景'));
        resume.addEventListener('click', function () { resumeRun(item); });
        historyList.appendChild(resume);
      }
      var exportBtn = el('button', 'run-row');
      exportBtn.type = 'button';
      exportBtn.appendChild(el('span', 'run-title', '导出 JSON（档案 + 看板 + 报告）'));
      exportBtn.appendChild(el('span', 'run-meta', '结构化结果，便于归档或接其它工具'));
      exportBtn.addEventListener('click', function () { exportRun(item); });
      historyList.appendChild(exportBtn);
    }).catch(function (err) {
      historyList.innerHTML = '';
      historyList.appendChild(el('p', 'drawer-note', err.message || '读取失败，请稍后再试。'));
    });
  }

  function exportRun(item) {
    if (!validRunId(item.id)) {
      addSystem('场次编号不合法，已忽略。', 'sys sys--error');
      return;
    }
    fetch('/api/runs/' + encodeURIComponent(item.id) + '/export').then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || ('导出失败 ' + res.status));
        return data;
      });
    }).then(function (data) {
      downloadJson(data, item.id + '.json');
      historyBox.hidden = true;
      addSystem('已导出场次 ' + item.id + ' 的结构化结果（JSON）', 'sys');
    }).catch(function (err) {
      addSystem('导出失败：' + err.message, 'sys sys--error');
    });
  }

  function loadRunFile(item, file) {
    if (!validRunId(item.id)) {
      addSystem('场次编号不合法，已忽略。', 'sys sys--error');
      return;
    }
    historyBox.hidden = true;
    var url = '/api/runs/' + encodeURIComponent(item.id)
      + '?kind=' + encodeURIComponent(file.kind || '');
    if (file.version) url += '&version=' + encodeURIComponent(file.version);
    fetch(url).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || ('读取失败 ' + res.status));
        return data;
      });
    }).then(function (data) {
      var card = el('article', 'msg msg--board');
      var head = el('header', 'msg-head');
      head.appendChild(el('span', 'who', '历史 · ' + (item.query || item.id)));
      head.appendChild(el('span', 'tag', runKindLabel(data.kind)
        + (data.version ? ' V' + data.version : '')));
      head.appendChild(downloadButton(data.text,
        item.id + (data.version ? '_V' + data.version : '') + '.txt'));
      if (data.kind === 'review') {
        var mark = el('button', 'mini', '标记已复查');
        mark.type = 'button';
        mark.addEventListener('click', function () {
          post('/api/runs/' + encodeURIComponent(item.id) + '/review', {}).then(function () {
            mark.textContent = '已复查';
            mark.disabled = true;
            addSystem('已标记复查：' + item.id, 'sys');
          }).catch(function (err) {
            addSystem('标记失败：' + err.message, 'sys sys--error');
          });
        });
        head.appendChild(mark);
      }
      card.appendChild(head);
      card.appendChild(el('div', 'msg-body', data.text || ''));
      append(card);
      addSystem('已载入历史场次 ' + item.id + '（只读）', 'sys');
    }).catch(function (err) {
      addError(err.message);
    });
  }

  function resumeRun(item) {
    if (!item.resumable) return;
    if (stage === 'running' || stage === 'stopping') {
      hint.textContent = '推演进行中，结束后再续跑。';
      return;
    }
    historyBox.hidden = true;
    stage = 'running';
    awaiting = false;
    setPhase(1);
    resetSeats();
    setModePill('续跑中', true);
    startWaitHint('正在恢复场次 ' + (item.version || '') + '…');
    post('/api/resume', { run_id: item.id }).then(function () {
      stream.innerHTML = '';
      renderQueue = [];
      pumping = false;
      paused = false;
      clearInput();
      connect(0);
    }).catch(function (err) {
      addError(err.message);
    });
  }

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  function petalNode(shape) {
    var petal = el('span', 'petal petal--' + shape);
    var sway = el('i', 'petal-sway');
    var flip = el('b', 'petal-flip');
    sway.appendChild(flip);
    petal.appendChild(sway);
    return petal;
  }

  function dropFinalPetal() {
    var layer = document.querySelector('.spring');
    var cards = stream.querySelectorAll('.msg--board');
    var card = cards.length ? cards[cards.length - 1] : null;
    if (!layer || !card || layer.querySelector('.petal--final')) return;
    var verdict = card.querySelector('.verdict') || card;
    var rect = verdict.getBoundingClientRect();
    var petal = petalNode('c');
    petal.className += ' petal--final';
    petal.style.setProperty('--fx', (rect.right - 46).toFixed(0) + 'px');
    petal.style.setProperty('--fy', (rect.top + 16).toFixed(0) + 'px');
    layer.appendChild(petal);
  }

  function seedPetals() {
    var back = document.querySelector('.spring--back');
    var front = document.querySelector('.spring--front');
    if (!back || !front || back.childElementCount || front.childElementCount) return;
    var wide = window.matchMedia('(min-width: 901px)').matches;
    var lowPower = (navigator.hardwareConcurrency || 8) <= 4 || (navigator.deviceMemory || 8) <= 4;
    var farTotal = wide ? (lowPower ? 18 : 26) : 10;
    var nearTotal = wide ? (lowPower ? 6 : 8) : 4;
    var shapes = ['a', 'b', 'c'];
    var i;
    for (i = 0; i < farTotal; i++) {
      var petal = petalNode(shapes[i % 3]);
      var left = i % 5 === 0;
      petal.className += ' petal--far';
      petal.style.setProperty('--x', (left ? rand(2, 40) : rand(45, 100)).toFixed(2) + '%');
      petal.style.setProperty('--size', (left ? rand(3, 5.5) : rand(4, 8)).toFixed(1) + 'px');
      petal.style.setProperty('--fall', rand(20, 34).toFixed(1) + 's');
      petal.style.setProperty('--sway', rand(3.2, 6.8).toFixed(1) + 's');
      petal.style.setProperty('--flip', rand(6, 11).toFixed(1) + 's');
      petal.style.setProperty('--amp', rand(6, 18).toFixed(0) + 'px');
      petal.style.setProperty('--blur', i % 5 === 0 ? rand(0.3, 0.6).toFixed(2) + 'px' : '0px');
      petal.style.setProperty('--op', (left ? rand(0.08, 0.16) : rand(0.1, 0.22)).toFixed(2));
      petal.style.setProperty('--delay', (-Math.random() * 36).toFixed(1) + 's');
      petal.style.setProperty('--flip-delay', (-Math.random() * 11).toFixed(1) + 's');
      back.appendChild(petal);
    }
    for (i = 0; i < nearTotal; i++) {
      var close = petalNode(shapes[(i + 1) % 3]);
      var closeSize = rand(9, 13);
      close.style.setProperty('--x', rand(76, 98).toFixed(2) + '%');
      close.style.setProperty('--size', closeSize.toFixed(1) + 'px');
      close.style.setProperty('--fall', rand(13, 22).toFixed(1) + 's');
      close.style.setProperty('--sway', rand(3.4, 7).toFixed(1) + 's');
      close.style.setProperty('--flip', rand(5, 9).toFixed(1) + 's');
      close.style.setProperty('--amp', rand(18, 44).toFixed(0) + 'px');
      close.style.setProperty('--blur', closeSize > 12 ? rand(0.3, 0.45).toFixed(2) + 'px' : '0px');
      close.style.setProperty('--op', (closeSize > 12 ? rand(0.24, 0.36) : rand(0.3, 0.42)).toFixed(2));
      close.style.setProperty('--delay', (-Math.random() * 24).toFixed(1) + 's');
      close.style.setProperty('--flip-delay', (-Math.random() * 9).toFixed(1) + 's');
      front.appendChild(close);
    }
    var rests = wide ? 10 : 6;
    for (i = 0; i < rests; i++) {
      var rest = petalNode(shapes[(i + 1) % 3]);
      rest.className += ' petal--rest';
      rest.style.setProperty('--x', rand(50, 98).toFixed(2) + '%');
      rest.style.setProperty('--size', rand(6, 12).toFixed(1) + 'px');
      rest.style.setProperty('--rest-y', rand(120, 200).toFixed(0) + 'px');
      rest.style.setProperty('--tilt', rand(-48, 48).toFixed(0) + 'deg');
      rest.style.setProperty('--op', rand(0.2, 0.36).toFixed(2));
      rest.style.setProperty('--blur', rand(0.2, 0.7).toFixed(2) + 'px');
      back.appendChild(rest);
    }
  }

  function seedCatkins() {
    var layer = document.querySelector('.spring--catkin');
    if (!layer || layer.childElementCount) return;
    var wide = window.matchMedia('(min-width: 901px)').matches;
    var lowPower = (navigator.hardwareConcurrency || 8) <= 4 || (navigator.deviceMemory || 8) <= 4;
    var total = wide ? (lowPower ? 6 : 8) : 4;
    for (var i = 0; i < total; i++) {
      var catkin = el('span', 'catkin');
      catkin.style.setProperty('--x', rand(4, 96).toFixed(2) + '%');
      catkin.style.setProperty('--size', rand(3.5, 7).toFixed(1) + 'px');
      catkin.style.setProperty('--dur', rand(22, 36).toFixed(1) + 's');
      catkin.style.setProperty('--amp', rand(28, 92).toFixed(0) + 'px');
      catkin.style.setProperty('--op', rand(0.2, 0.4).toFixed(2));
      catkin.style.setProperty('--blur', rand(0.1, 0.3).toFixed(2) + 'px');
      catkin.style.setProperty('--delay', (-Math.random() * 36).toFixed(1) + 's');
      layer.appendChild(catkin);
    }
  }

  seedPetals();
  seedCatkins();
  scheduleGust();

  historyBtn.addEventListener('click', openHistory);
  historyClose.addEventListener('click', function () { historyBox.hidden = true; });

  if (skipBtn) {
    skipBtn.addEventListener('click', function () { setFast(true); });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) setFast(true);
  });
})();