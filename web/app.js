/* Magia☆Record 存档编辑器 —— 前端（原生 JS，无构建） */
(function () {
  'use strict';

  var S = { status: null, tab: 'overview', page: {}, busy: false, piecePicker: null, charaSel: null, charaPanel: null,
    spiritSel: null, pieceSel: null, deckSel: null, chapterMap: null, sectionSel: null,
    pieceFilter: { rank: '', type: '', limit: '', charaId: '' },
    itemsNav: { family: null, group: null }, open: {} };

  var TABS = [
    { id: 'overview', label: '概览' },
    { id: 'resources', label: '资源与状态' },
    { id: 'items', label: '道具' },
    { id: 'charas', label: '魔法少女' },
    { id: 'pieces', label: '记忆结晶' },
    { id: 'decks', label: '队伍' },
    { id: 'chapters', label: '剧情关卡' },
    { id: 'challenges', label: '挑战' },
    { id: 'gifts', label: '养成素材（gift）' },
    { id: 'raw', label: '原始 JSON' }
  ];

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  // 游戏文本用「＠」做换行，前端统一转成 \n（配合 white-space: pre-wrap）
  function nl(text) {
    return String(text === null || text === undefined ? '' : text).replace(/[＠@]/g, '\n');
  }

  function fmt(value) {
    if (value === null || value === undefined) { return '—'; }
    if (typeof value === 'number') { return value.toLocaleString('zh-CN'); }
    return esc(value);
  }
  function thumb(url, label, cls) {
    if (!url) { return ''; }
    return '<img class="' + (cls || 'thumb') + '" loading="lazy" src="' + esc(url) + '" alt="' + esc(label || '') + '"'
      + ' onerror="this.classList.add(\'missing\');this.removeAttribute(\'src\')">';
  }

  function api(path, body) {
    var options = { headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () {
        throw new Error('服务返回非 JSON（HTTP ' + response.status + '）');
      }).then(function (payload) {
        if (payload && payload.ok === false) { throw new Error(payload.error || ('请求失败: ' + path)); }
        return payload;
      });
    });
  }

  var toastTimer = null;
  function toast(message, kind) {
    var node = $('toast');
    node.textContent = message;
    node.className = 'toast show ' + (kind || '');
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { node.className = 'toast'; }, kind === 'err' ? 8000 : 3000);
  }
  function fail(error) { toast(error && error.message ? error.message : String(error), 'err'); }

  function pageOf(tabId) {
    if (!S.page[tabId]) { S.page[tabId] = { offset: 0, q: '', only: '' }; }
    return S.page[tabId];
  }

  // ------------------------------------------------------------ 顶部

  function renderMeta() {
    var status = S.status;
    var meta = $('archiveMeta');
    var badge = $('dirtyBadge');
    if (!status || !status.loaded) {
      meta.textContent = '未打开存档';
      badge.className = 'badge off';
      badge.textContent = '无改动';
      return;
    }
    var summary = status.summary;
    meta.textContent = [summary.label, '模板 ' + summary.template, 'revision ' + summary.revision,
      'bank ' + summary.bank, summary.parts + ' 片 / ' + summary.bytes.toLocaleString('zh-CN') + ' 字节',
      summary.encoding ? '编码 ' + summary.encoding : '存储 raw', summary.path].join(' · ');
    if (status.dirty) {
      badge.className = 'badge dirty';
      badge.textContent = '未保存改动（撤销栈 ' + status.undoDepth + '）';
    } else {
      badge.className = 'badge on';
      badge.textContent = '未改动';
    }
  }

  function refreshStatus() {
    return api('/api/status').then(function (payload) { S.status = payload; renderMeta(); return payload; });
  }

  function renderTabs() {
    var nav = $('tabs');
    nav.innerHTML = '';
    TABS.forEach(function (tab) {
      var button = document.createElement('button');
      button.textContent = tab.label;
      button.className = S.tab === tab.id ? 'active' : '';
      button.addEventListener('click', function () {
        if (S.tab === tab.id) { return; }
        S.tab = tab.id;
        S.charaSel = null;
        S.charaPanel = null;
        S.pieceSel = null;
        S.deckSel = null;
        S.chapterMap = null;
        S.sectionSel = null;
        S.spiritSel = null;
        renderTabs();
        render();
      });
      nav.appendChild(button);
    });
  }

  // ------------------------------------------------------------ 地址路由（浏览器前进 / 后退）

  var suppressHash = false;

  function stateHash() {
    var parts = [S.tab];
    if (S.tab === 'charas' && S.charaSel) {
      parts.push(encodeURIComponent(S.charaSel));
      if (S.charaPanel) { parts.push(S.charaPanel); }
    } else if (S.tab === 'pieces' && S.pieceSel) {
      parts.push(encodeURIComponent(S.pieceSel));
    } else if (S.tab === 'decks' && S.deckSel) {
      parts.push(encodeURIComponent(S.deckSel));
    } else if (S.tab === 'chapters' && S.chapterMap) {
      parts.push(encodeURIComponent(S.chapterMap));
      if (S.sectionSel) { parts.push(encodeURIComponent(S.sectionSel)); }
    }
    return '#/' + parts.join('/');
  }

  function syncHash() {
    var target = stateHash();
    if (window.location.hash === target) { return; }
    suppressHash = true;
    window.location.hash = target;
  }

  function applyHash() {
    var raw = String(window.location.hash || '').replace(/^#\/?/, '');
    var parts = raw.split('/').filter(function (item) { return item; });
    var tab = parts[0] || 'overview';
    var known = TABS.filter(function (item) { return item.id === tab; });
    if (!known.length) { tab = 'overview'; }
    S.tab = tab;
    S.spiritSel = null;
    S.charaSel = (tab === 'charas' && parts[1]) ? decodeURIComponent(parts[1]) : null;
    S.charaPanel = (tab === 'charas' && parts[2]) ? parts[2] : null;
    S.pieceSel = (tab === 'pieces' && parts[1]) ? decodeURIComponent(parts[1]) : null;
    S.deckSel = (tab === 'decks' && parts[1]) ? decodeURIComponent(parts[1]) : null;
    S.chapterMap = (tab === 'chapters' && parts[1]) ? decodeURIComponent(parts[1]) : null;
    S.sectionSel = (tab === 'chapters' && parts[2]) ? decodeURIComponent(parts[2]) : null;
    SCROLL.key = null;
    SCROLL.y = 0;
    renderTabs();
    render();
  }

  // 点击不会把页面弹回顶部：同一视图内重绘时记住并恢复滚动位置。
  var SCROLL = { key: null, y: 0, ready: false };

  function viewKey() {
    var page = S.page[S.tab] || {};
    return [S.tab, S.charaSel || '', S.charaPanel || '', S.itemsNav.family || '',
      S.itemsNav.group || '', page.offset || 0, page.q || ''].join('|');
  }

  function windowScrollY() {
    return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;
  }

  function restoreScroll(epoch) {
    var y = SCROLL.key === viewKey() ? SCROLL.y : 0;
    window.scrollTo(0, y);
    SCROLL.ready = true;
    if (!y) { return; }
    // 内容高度在下一帧才稳定（图片 / SVG 布局），再校一次。
    window.requestAnimationFrame(function () {
      if (epoch !== renderEpoch) { return; }
      var max = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
      window.scrollTo(0, Math.min(y, Math.max(0, max - window.innerHeight)));
    });
  }

  // Only the latest navigation may paint. Let older reads finish without touching the visible form.
  var renderEpoch = 0;
  function paintCurrent(promise, draw) {
    var epoch = renderEpoch;
    return promise.then(function (value) {
      if (epoch === renderEpoch) { return draw(value); }
    });
  }

  function render() {
    var epoch = ++renderEpoch;
    if (!S.status || !S.status.loaded) {
      $('main').innerHTML = '<div class="empty" id="emptyHint">请先打开一个存档 zip。</div>';
      return;
    }
    syncHash();
    var nextKey = viewKey();
    // 只在内容完整时读取当前滚动位置；连续点击期间的「加载中」不会把 y 冲成 0。
    if (SCROLL.key === nextKey && SCROLL.ready) { SCROLL.y = windowScrollY(); }
    if (SCROLL.key !== nextKey) { SCROLL.y = 0; }
    SCROLL.key = nextKey;
    SCROLL.ready = false;
    var host = $('main');
    host.innerHTML = '<div class="empty">加载中…</div>';
    var task = Promise.resolve();
    switch (S.tab) {
      case 'overview': task = renderOverview(); break;
      case 'resources': task = renderResources(); break;
      case 'items': task = renderItems(); break;
      case 'charas': task = renderCharas(); break;
      case 'pieces': task = renderPieces(); break;
      case 'decks': task = renderDecks(); break;
      case 'quests': task = renderQuests(); break;
      case 'chapters': task = renderChapters(); break;
      case 'challenges': task = renderChallenges(); break;
      case 'gifts': task = renderGifts(); break;
      case 'raw': task = renderRaw(); break;
      default: task = Promise.resolve();
    }
    return task.then(function () {
      if (epoch === renderEpoch) { restoreScroll(epoch); }
    }, function (error) {
      if (epoch !== renderEpoch) { return; }
      host.innerHTML = '<div class="empty">加载失败：' + esc(error.message) + '</div>';
      restoreScroll(epoch);
    });
  }

  // ------------------------------------------------------------ 编辑

  function edit(ops) {
    if (S.busy) { return Promise.resolve(false); }
    S.busy = true;
    return api('/api/edit', { ops: ops }).then(function (result) {
      S.busy = false;
      if (result.noop) { toast('数值没有变化'); return false; }
      toast('已应用 ' + result.changed.length + ' 处改动（未落盘）', 'ok');
      return refreshStatus().then(function () { render(); return true; });
    }).catch(function (error) {
      S.busy = false;
      fail(error);
      return false;
    });
  }

  // Only typed decimal integers may change a quantity. An empty number field
  // can also mean the browser is still holding an incomplete token (e.g. "1e").
  function readInteger(input, minimum, maximum) {
    var field = typeof input === 'string' ? null : input;
    var raw = (field ? field.value : input).trim();
    if (minimum == null) { minimum = field && field.min ? Number(field.min) : 0; }
    if (maximum == null) { maximum = field && field.max ? Number(field.max) : null; }
    var value = Number(raw);
    if ((field && field.validity && field.validity.badInput) || !/^[0-9]+$/.test(raw)
        || !Number.isSafeInteger(value) || value < minimum || (maximum != null && value > maximum)) {
      fail(new Error('请输入' + minimum + (maximum == null ? '或以上' : '至' + maximum) + '的整数'));
      if (field) { field.focus(); }
      return null;
    }
    return value;
  }

  function bindNumberRows(root, handler) {
    root.querySelectorAll('[data-apply]').forEach(function (button) {
      button.addEventListener('click', function () {
        var row = button.closest('[data-spec]');
        if (!row) { return; }
        handler(JSON.parse(row.getAttribute('data-spec')), row, button);
      });
    });
  }

  // ------------------------------------------------------------ 分页（渲染后统一绑定）

  function pagerHtml(total, limit, page) {
    var offset = page.offset || 0;
    var html = '<div class="pager">';
    html += '<button class="mini" data-page="prev"' + (offset <= 0 ? ' disabled' : '') + '>上一页</button>';
    html += '<span>' + (total === 0 ? 0 : offset + 1) + ' – ' + Math.min(offset + limit, total) + ' / ' + total + '</span>';
    html += '<button class="mini" data-page="next"' + (offset + limit >= total ? ' disabled' : '') + '>下一页</button>';
    return html + '</div>';
  }

  function bindPager(tabId, limit, reload) {
    var page = pageOf(tabId);
    var prev = $('main').querySelector('[data-page="prev"]');
    var next = $('main').querySelector('[data-page="next"]');
    if (prev && !prev.disabled) {
      prev.addEventListener('click', function () {
        page.offset = Math.max(0, (page.offset || 0) - limit);
        reload();
      });
    }
    if (next && !next.disabled) {
      next.addEventListener('click', function () {
        page.offset = (page.offset || 0) + limit;
        reload();
      });
    }
  }

  function searchBox(tabId, placeholder, reload) {
    var page = pageOf(tabId);
    var input = $('main').querySelector('[data-search]');
    if (!input) { return; }
    input.value = page.q || '';
    var timer = null;
    input.addEventListener('input', function () {
      if (timer) { clearTimeout(timer); }
      timer = setTimeout(function () {
        page.q = input.value.trim();
        page.offset = 0;
        reload();
      }, 260);
    });
  }

  // ------------------------------------------------------------ 概览

  function renderOverview() {
    return paintCurrent(api('/api/overview?diff=0'), function (data) {
      var player = data.player;
      var currency = data.currency;
      var counts = data.counts;
      var archive = data.archive;
      var html = '<div class="cards">';
      html += card('玩家', [['昵称', player.loginName], ['等级', player.level], ['经验', player.exp],
        ['升级所需', player.expForNext], ['卡组', player.deckType], ['创建', player.createdAt]]);
      html += card('货币与资源', [['金币（riche）', currency.riche], ['魔法石', currency.money],
        ['礼物魔法石', currency.presentedMoney], ['协助Pt', currency.yell], ['镜币', currency.arenaCoin],
        ['扭蛋券', currency.gachaTicket]]);
      html += card('进度', [['已通关任务', counts.questCleared + ' / ' + counts.quest],
        ['可玩小节', counts.sectionCanPlay + ' / ' + counts.section],
        ['已通章节', counts.chapterCleared + ' / ' + counts.chapter],
        ['角色 / 卡片', counts.chara + ' / ' + counts.card], ['记忆结晶', counts.piece],
        ['养成素材', counts.gift]]);
      html += card('存档容器', [['模板', archive.template], ['标签', archive.label],
        ['revision', archive.revision], ['分片', archive.parts + ' 片 · bank ' + archive.bank],
        ['profile 字节', archive.profileBytes], ['存储编码', archive.encoding || 'raw']]);
      html += '</div>';

      html += '<section class="panel"><h2>脱战'
        + (data.battle && data.battle.stuck ? ' <span class="tag warn">有未完成的战斗</span>' : '')
        + '</h2><p class="small">异常退出或战斗中断后，存档里可能残留未完成的战斗'
        + '（<code>battleSession</code> 与 <code>gameUser.userQuestBattleResultId</code>），'
        + '游戏会反复要求续战甚至进不去。点下面按钮把这次战斗清掉：只丢掉未完成的那一场，'
        + '已结算的战斗记录、奖励与关卡进度都不动。</p>'
        + '<div class="row"><button class="mini" data-battle-clear>清除未完成战斗（脱战）</button></div></section>';

      html += '<section class="panel"><h2>只读与推算规则</h2><p class="small">'
        + '卡片与记忆结晶的 attack / defense / hp 由等级、突破按游戏公式推算，不能直接改；'
        + '以下集合本编辑器完全不动：' + esc((data.readonly.collections || []).join('、')) + '</p>'
        + '<p class="small">成长数据：卡片 ' + esc(String(data.masters.cards)) + ' 条、结晶 '
        + esc(String(data.masters.pieces)) + ' 条；主数据：道具 ' + esc(String(data.catalog.items))
        + '、角色 ' + esc(String(data.catalog.chara)) + '、卡 ' + esc(String(data.catalog.cards))
        + '、结晶 ' + esc(String(data.catalog.pieces)) + '</p></section>';

      html += '<section class="panel"><h2>状态点</h2><table><thead><tr><th>ID</th><th>含义</th><th>当前</th></tr></thead><tbody>';
      data.status.forEach(function (row) {
        html += '<tr><td>' + esc(row.statusId) + '</td><td>' + esc(row.label) + '</td><td class="num">'
          + fmt(row.point) + '</td></tr>';
      });
      html += '</tbody></table></section>';
      $('main').innerHTML = html;
      var battleClear = $('main').querySelector('[data-battle-clear]');
      if (battleClear) {
        battleClear.addEventListener('click', function () {
          edit([{ op: 'battle.clear' }]);
        });
      }
    });
  }

  function card(title, rows) {
    var html = '<div class="card"><h3>' + esc(title) + '</h3>';
    rows.forEach(function (pair) {
      html += '<div class="kv"><span>' + esc(pair[0]) + '</span><strong>' + fmt(pair[1]) + '</strong></div>';
    });
    return html + '</div>';
  }

  // ------------------------------------------------------------ 资源与状态

  function renderResources() {
    return paintCurrent(Promise.all([api('/api/overview?diff=0'), api('/api/view/status')]), function (results) {
      var data = results[0];
      var statusRows = results[1].rows;
      var currency = data.currency;
      var player = results[1].player || {};
      var html = '<section class="panel"><h2>玩家</h2>'
        + textField({ op: 'player.set', field: 'loginName', valueKey: 'value', value: player.loginName }, '昵称（user.loginName）')
        + textField({ op: 'player.set', field: 'comment', valueKey: 'value', value: player.comment }, '简介（gameUser.comment）')
        + '<p class="small">玩家 ID 是 ' + esc(player.userId) + '（不可修改）。</p></section>';
      html += '<section class="panel"><h2>货币 / 关键资源</h2><p class="small">'
        + '名称取自官方主数据；修改后即时写入内存，点右上「另存为…」才产出新存档。</p>';
      var money = [
        ['riche', '金币（gameUser.riche）', currency.riche],
        ['MONEY', '魔法石（MONEY）', currency.money],
        ['PRESENTED_MONEY', '礼物魔法石（PRESENTED_MONEY）', currency.presentedMoney],
        ['YELL', '协助Pt（YELL）', currency.yell],
        ['ARENA_COIN', '镜币（ARENA_COIN）', currency.arenaCoin],
        ['PRISM', '魔法晶片（PRISM）', currency.prism],
        ['GACHA_TICKET', '扭蛋券（GACHA_TICKET）', currency.gachaTicket],
        ['SCENEZERO_PLAY_TICKET', '回溯灯泡（SCENEZERO_PLAY_TICKET）', currency.sceneZeroTicket]
      ];
      money.forEach(function (row) {
        html += numberRow({ op: 'money.set', kind: row[0], valueKey: 'value', value: row[2] }, row[1]);
      });
      html += '</section>';

      var medals = results[1].groupBattleMedals || [];
      if (medals.length) {
        html += '<section class="panel"><h2>心魔奖章（团战奖章） <span class="small">'
          + medals.length + ' 项</span></h2>'
          + '<p class="small">每个团战活动一种奖章，改数量即时生效；没有的活动显示 0，填数字即可发放。</p>'
          + '<details><summary class="small">展开 / 收起各项数量</summary>';
        medals.forEach(function (row) {
          html += numberRow({ op: 'item.set', itemId: row.itemId, valueKey: 'quantity', value: row.quantity },
            row.name + '（活动 ' + row.eventId + '）');
        });
        html += '</details></section>';
      }

      html += '<section class="panel"><h2>状态点（AP / BP / CP / RP 与属性强化）</h2>';
      var maxOf = {};
      statusRows.forEach(function (row) {
        if (row.maxOf) { maxOf[row.maxOf] = row.statusId; }
      });
      // 只有「被主项当作上限引用」的 MAX_* 才隐藏（值显示在主行的上限里）；MAX_FOLLOW 这类独立项要保留
      var usedAsMax = {};
      statusRows.forEach(function (row) { if (row.maxOf) { usedAsMax[row.maxOf] = true; } });
      statusRows.forEach(function (row) {
        if (usedAsMax[row.statusId]) { return; }
        var spec = { op: 'status.set', statusId: row.statusId, valueKey: 'point' };
        var maxRow = row.maxOf
          ? statusRows.filter(function (item) { return item.statusId === row.maxOf; })[0]
          : null;
        var html2 = '<div class="fieldRow" data-spec=\'' + esc(JSON.stringify(spec)) + '\'>'
          + '<label>' + esc(row.statusId) + ' · ' + esc(row.label) + '</label>'
          + '<input data-field type="number" min="0" value="' + esc(row.point) + '">';
        if (maxRow) {
          html2 += '<span class="small">上限</span><input data-max-field type="number" min="0" value="'
            + esc(maxRow.point) + '">';
        }
        html2 += '<button class="mini" data-apply>应用</button></div>';
        html += html2;
      });
      html += '<div class="toolbar" style="margin-top:12px">'
        + '<button data-preset="ap">AP / BP 拉满</button>'
        + '<button data-preset="arena">CP / RP 拉满</button></div></section>';

      $('main').innerHTML = html;
      bindNumberRows($('main'), function (spec, row) {
        var input = row.querySelector('[data-field]');
        if (input && input.type === 'text') {
          var textOp = JSON.parse(JSON.stringify(spec));
          textOp[spec.valueKey || 'value'] = input.value;
          edit([textOp]);
          return;
        }
        var value = readInteger(input);
        if (value === null) { return; }
        var op = JSON.parse(JSON.stringify(spec));
        op[spec.valueKey] = value;
        var maxInput = row.querySelector('[data-max-field]');
        if (maxInput && (maxInput.value !== '' || maxInput.validity.badInput)) {
          op.maxPoint = readInteger(maxInput);
          if (op.maxPoint === null) { return; }
        }
        edit([op]);
      });
      $('main').querySelectorAll('[data-preset]').forEach(function (button) {
        button.addEventListener('click', function () {
          var kind = button.getAttribute('data-preset');
          var ops = [];
          if (kind === 'ap') {
            ops.push({ op: 'status.set', statusId: 'ACP', point: 999, maxPoint: 999 });
            ops.push({ op: 'status.set', statusId: 'BTP', point: 999, maxPoint: 999 });
          } else {
            ops.push({ op: 'status.set', statusId: 'CP', point: 999, maxPoint: 999 });
            ops.push({ op: 'status.set', statusId: 'RP', point: 999, maxPoint: 999 });
          }
          edit(ops);
        });
      });
    });
  }

  function textField(spec, label) {
    return '<div class="fieldRow" data-spec=\'' + esc(JSON.stringify(spec)) + '\'>'
      + '<label>' + esc(label) + '</label>'
      + '<input data-field type="text" value="'
      + esc(spec.value === null || spec.value === undefined ? '' : spec.value) + '">'
      + '<button class="mini" data-apply>应用</button></div>';
  }

  function numberRow(spec, label, extra) {
    return '<div class="fieldRow" data-spec=\'' + esc(JSON.stringify(spec)) + '\'>'
      + '<label>' + esc(label) + '</label>'
      + '<input data-field type="number" min="0" value="'
      + esc(spec.value === null || spec.value === undefined ? '' : spec.value) + '">'
      + (extra || '') + '<button class="mini" data-apply>应用</button></div>';
  }

  // ------------------------------------------------------------ 页内选择器（模态框）

  var pickerOpener = null, previewOpener = null, inertForDialog = [];
  function topDialog() { return $('previewModal') || ($('modal').classList.contains('show') ? $('modal') : null); }
  function syncDialogLayer() {
    inertForDialog.forEach(function (node) { node.inert = false; });
    inertForDialog = [];
    var active = topDialog();
    if (!active) { return; }
    Array.prototype.forEach.call(document.body.children, function (node) {
      if (node !== active && node.id !== 'toast' && node.tagName !== 'SCRIPT' && !node.inert && 'inert' in node) {
        node.inert = true; inertForDialog.push(node);
      }
    });
  }
  function focusInView(node) {
    if (node && node.isConnected && !node.disabled && node.getClientRects().length) {
      node.focus({ preventScroll: true }); return document.activeElement === node;
    }
    return false;
  }
  function dialogKeys(event) {
    var layer = topDialog(); if (!layer || event.isComposing || event.keyCode === 229) { return; }
    if (event.key === 'Escape') {
      event.preventDefault();
      if (layer.id === 'previewModal') { closePreview(); } else { closeModal(); }
    } else if (event.key === 'Tab') {
      var nodes = layer.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex]');
      var first = null, last = null, i;
      for (i = 0; i < nodes.length; i++) {
        if (nodes[i].tabIndex >= 0 && nodes[i].getClientRects().length) { first = nodes[i]; break; }
      }
      for (i = nodes.length - 1; i >= 0; i--) {
        if (nodes[i].tabIndex >= 0 && nodes[i].getClientRects().length) { last = nodes[i]; break; }
      }
      if (first && (!layer.contains(document.activeElement) || (event.shiftKey ? document.activeElement === first : document.activeElement === last))) {
        event.preventDefault(); (event.shiftKey ? last : first).focus();
      }
    }
  }

  function closeModal() {
    var host = $('modal');
    if (!host.classList.contains('show')) { return; }
    host.classList.remove('show');
    syncDialogLayer();
    if (!focusInView(pickerOpener)) { focusInView(document.querySelector('#tabs .active')); }
    pickerOpener = null;
    host.setAttribute('aria-hidden', 'true');
    $('modalSearch').oninput = null;
  }

  function openModal(title, placeholder, searchable) {
    var host = $('modal');
    if (!host.classList.contains('show')) { pickerOpener = document.activeElement; }
    host.classList.toggle('picker-searchable', searchable);
    host.querySelector('[role="dialog"]').setAttribute('aria-labelledby', 'modalTitle');
    $('modalTitle').textContent = title;
    var input = $('modalSearch');
    input.classList.toggle('hidden', !searchable);
    input.value = '';
    input.placeholder = placeholder || '搜索名称或 ID';
    $('modalMeta').textContent = '';
    $('modalBody').innerHTML = '<div class="empty">加载中…</div>';
    host.classList.add('show');
    host.setAttribute('aria-hidden', 'false');
    syncDialogLayer();
    focusInView(searchable ? input : $('modalClose'));
    return host;
  }

  /* config: { title, placeholder, load(q), meta(data), render(rows), bind(host, rows) } */
  function openPicker(config) {
    openModal(config.title, config.placeholder, config.search !== false);
    function load(query) {
      $('modalBody').innerHTML = '<div class="empty">加载中…</div>';
      config.load(query).then(function (data) {
        $('modalMeta').textContent = config.meta ? config.meta(data) : '';
        var rows = (data && data.rows) || [];
        if (!rows.length) {
          $('modalBody').innerHTML = '<div class="empty">没有匹配项</div>';
          return;
        }
        $('modalBody').innerHTML = config.render(rows, data);
        if (config.bind) { config.bind($('modalBody'), rows, data); }
      }).catch(function (error) {
        $('modalBody').innerHTML = '<div class="empty">加载失败：' + esc(error.message) + '</div>';
      });
    }
    var timer = null;
    $('modalSearch').oninput = function () {
      if (timer) { clearTimeout(timer); }
      timer = setTimeout(function () { load($('modalSearch').value.trim()); }, 240);
    };
    load('');
    // 弹窗内部的筛选控件靠这个重新拉数据（不关闭弹窗）
    $('modalBody')._reload = function () { load($('modalSearch').value.trim()); };
  }

  function tileHtml(icon, name, idText, bodyHtml, tagsHtml) {
    return '<div class="tile">'
      + (icon ? icon : '<img class="icon missing" alt="">')
      + '<div class="name" title="' + esc(name) + '">' + esc(name) + '</div>'
      + '<div class="id">' + esc(idText || '') + '</div>'
      + (tagsHtml ? '<div class="tags">' + tagsHtml + '</div>' : '')
      + bodyHtml
      + '</div>';
  }

  function statLine(items) {
    var html = '<div class="stat">';
    items.forEach(function (pair) {
      html += '<span>' + esc(pair[0]) + '</span><b>' + fmt(pair[1]) + '</b>';
    });
    return html + '</div>';
  }

  // ------------------------------------------------------------ 道具

  function itemCrumb() {
    var nav = S.itemsNav || (S.itemsNav = { family: null, group: null });
    var html = '<div class="crumbs">';
    html += '<button class="mini" data-crumb="root"' + (nav.family ? '' : ' disabled') + '>全部分类</button>';
    if (nav.family) {
      html += '<span>›</span><button class="mini" data-crumb="family">'
        + esc(nav.familyLabel || nav.family) + '</button>';
    }
    if (nav.group) {
      html += '<span>›</span><span class="small">' + esc(nav.groupLabel || nav.group) + '</span>';
    }
    return html + '</div>';
  }

  function bindCrumb() {
    var nav = S.itemsNav || (S.itemsNav = { family: null, group: null });
    var root = $('main').querySelector('[data-crumb="root"]');
    if (root) {
      root.addEventListener('click', function () {
        S.itemsNav = { family: null, group: null, scope: nav.scope || 'player' };
        S.page.items = { offset: 0, q: '', sort: 'id' };
        render();
      });
    }
    var family = $('main').querySelector('[data-crumb="family"]');
    if (family) {
      family.addEventListener('click', function () {
        nav.group = null;
        S.page.items = { offset: 0, q: '', sort: 'id' };
        render();
      });
    }
  }

  function renderItems() {
    var nav = S.itemsNav || (S.itemsNav = { family: null, group: null });
    if (nav.search) { return renderItemList(null); }
    if (!nav.family) { return renderItemFamilies(); }
    if (!nav.group) { return renderItemGroups(nav.family); }
    return renderItemList(nav.group);
  }

  function renderItemFamilies() {
    var scope = (S.itemsNav && S.itemsNav.scope) || 'player';
    return paintCurrent(api('/api/view/items-families?scope=' + scope), function (data) {
      var html = '<div class="toolbar">' + itemCrumb()
        + '<input type="text" data-global-search placeholder="全局搜索道具（名称 / itemId），回车确认">'
        + '<select data-scope><option value="player">玩家可见道具</option>'
        + '<option value="all">全部（含内部道具）</option></select>'
        + '<span class="spacer"></span>'
        + '<span class="small">' + data.totalItems + ' 种道具，按来源活动分 ' + data.families.length
        + ' 类</span></div><div class="grid wide">';
      data.families.forEach(function (family) {
        html += '<div class="tile cat" data-family="' + esc(family.key) + '">'
          + '<div class="name">' + esc(family.label) + '</div>'
          + '<div class="id">' + family.itemCount + ' 种 · ' + family.groupCount + ' 组活动</div>'
          + statLine([['已拥有', family.owned]])
          + '</div>';
      });
      html += '</div>';
      $('main').innerHTML = html;
      bindCrumb();
      var globalSearch = $('main').querySelector('[data-global-search]');
      if (globalSearch) {
        globalSearch.addEventListener('keydown', function (event) {
          if (event.key !== 'Enter') { return; }
          var term = globalSearch.value.trim();
          if (!term) { return; }
          S.itemsNav = { family: null, group: null, scope: scope, search: term };
          S.page.items = { offset: 0, q: term, sort: 'qty' };
          render();
        });
      }
      var scopeSelect = $('main').querySelector('[data-scope]');
      if (scopeSelect) {
        scopeSelect.value = scope;
        scopeSelect.addEventListener('change', function () {
          S.itemsNav = { family: null, group: null, scope: scopeSelect.value };
          S.page.items = { offset: 0, q: '', sort: 'id' };
          render();
        });
      }
      $('main').querySelectorAll('[data-family]').forEach(function (tile) {
        tile.addEventListener('click', function () {
          S.itemsNav = {
            family: tile.getAttribute('data-family'),
            familyLabel: tile.querySelector('.name').textContent,
            group: null,
            scope: scope
          };
          S.page.items = { offset: 0, q: '', sort: 'id' };
          render();
        });
      });
    });
  }

  function renderItemGroups(family) {
    var scope = (S.itemsNav && S.itemsNav.scope) || 'player';
    return paintCurrent(api('/api/view/items-groups?family=' + encodeURIComponent(family)
      + '&scope=' + scope), function (data) {
      var html = '<div class="toolbar">' + itemCrumb()
        + '<input type="text" data-filter placeholder="过滤活动名">'
        + '<span class="spacer"></span><span class="small">' + data.total + ' 组活动</span></div>';
      html += '<div class="grid wide">';
      data.groups.forEach(function (group) {
        html += '<div class="tile cat" data-group="' + esc(group.key) + '" data-label="'
          + esc(group.label) + '">'
          + '<div class="name">' + esc(group.label) + '</div>'
          + statLine([['道具', group.count]])
          + statLine([['已拥有', group.owned]])
          + '</div>';
      });
      html += '</div>';
      $('main').innerHTML = html;
      bindCrumb();
      var filter = $('main').querySelector('[data-filter]');
      filter.addEventListener('input', function () {
        var needle = filter.value.trim().toLowerCase();
        $('main').querySelectorAll('.tile.cat').forEach(function (tile) {
          var label = (tile.getAttribute('data-label') || '').toLowerCase();
          tile.style.display = !needle || label.indexOf(needle) >= 0 ? '' : 'none';
        });
      });
      $('main').querySelectorAll('[data-group]').forEach(function (tile) {
        tile.addEventListener('click', function () {
          var nav = S.itemsNav || (S.itemsNav = {});
          nav.group = tile.getAttribute('data-group');
          nav.groupLabel = tile.getAttribute('data-label');
          nav.scope = nav.scope || 'player';
          S.page.items = { offset: 0, q: '', sort: 'id' };
          render();
        });
      });
    });
  }

  function renderItemList(group) {
    var tabId = 'items';
    var page = pageOf(tabId);
    var limit = 200;
    var sort = page.sort || 'id';
    var searching = !group && S.itemsNav && S.itemsNav.search;
    var url = '/api/view/items?scope=' + ((S.itemsNav && S.itemsNav.scope) || 'player')
      + (group ? '&group=' + encodeURIComponent(group) : '')
      + '&offset=' + page.offset
      + '&limit=' + limit + '&sort=' + sort + '&q=' + encodeURIComponent(page.q || '');
    return paintCurrent(api(url), function (data) {
      var owned = data.rows.filter(function (row) { return row.owned; }).length;
      var html = '<div class="toolbar">' + itemCrumb()
        + (searching ? '<span class="small">搜索「' + esc(S.itemsNav.search) + '」</span>'
          + '<button class="mini" data-search-clear>返回分类</button>' : '')
        + '<input type="text" data-search placeholder="搜索道具名称或 itemId">'
        + '<select data-sort><option value="qty">按数量</option><option value="name">按名称</option>'
        + '<option value="id">按 ID</option></select><span class="spacer"></span>'
        + '<span class="small">' + (searching ? '匹配 ' : '本组 ') + data.total
        + ' 种（本页已有 ' + owned + ' 种）</span></div>';
      html += '<div class="grid">';
      data.rows.forEach(function (row) {
        html += tileHtml(
          thumb(row.icon, row.name, 'icon'),
          row.name,
          row.itemId + (row.type ? ' · ' + row.type : ''),
          '<div class="row"><input data-qty type="number" min="0" value="' + esc(row.quantity) + '">'
            + '<button class="mini" data-save>应用</button></div>'
            + statLine([['累计', row.total]]),
          (row.owned ? '<span class="tag ok">已拥有</span>' : '<span class="tag">未拥有</span>')
        );
      });
      html += '</div>' + pagerHtml(data.total, limit, page);
      $('main').innerHTML = html;
      bindCrumb();
      searchBox(tabId, '搜索道具', render);
      bindPager(tabId, limit, render);
      var clearSearch = $('main').querySelector('[data-search-clear]');
      if (clearSearch) {
        clearSearch.addEventListener('click', function () {
          S.itemsNav = { family: null, group: null,
            scope: (S.itemsNav && S.itemsNav.scope) || 'player' };
          S.page.items = { offset: 0, q: '', sort: 'id' };
          render();
        });
      }
      var sortSelect = $('main').querySelector('[data-sort]');
      sortSelect.value = sort;
      sortSelect.addEventListener('change', function () {
        page.sort = sortSelect.value;
        page.offset = 0;
        render();
      });
      var rows = data.rows;
      $('main').querySelectorAll('[data-save]').forEach(function (button, index) {
        button.addEventListener('click', function () {
          var input = button.closest('.tile').querySelector('[data-qty]');
          var value = readInteger(input);
          if (value === null) { return; }
          edit([{ op: 'item.set', itemId: rows[index].itemId, quantity: value }]);
        });
      });
    });
  }

  function openItemPicker() {
    openPicker({
      title: '添加道具',
      placeholder: '搜索道具名称或 itemId（留空列出常用条目）',
      load: function (q) { return api('/api/catalog/items?limit=120&q=' + encodeURIComponent(q)); },
      meta: function (data) { return '匹配 ' + data.total + ' 条，按名称选择后填数量'; },
      render: function (rows) {
        var html = '<div class="grid">';
        rows.forEach(function (row) {
          html += tileHtml(
            thumb('/assets/items/' + String(row.itemId).toLowerCase() + '.png', row.name, 'icon'),
            row.name,
            row.itemId + (row.unit ? ' · ' + row.unit : ''),
            '<div class="row"><input data-qty type="number" min="0" value="1">'
              + '<button class="mini" data-add>添加</button></div>'
          );
        });
        return html + '</div>';
      },
      bind: function (host, rows) {
        host.querySelectorAll('[data-add]').forEach(function (button, index) {
          button.addEventListener('click', function (event) {
            var input = button.closest('.tile').querySelector('[data-qty]');
            var value = readInteger(input);
            if (value === null) { event.preventDefault(); return; }
            edit([{ op: 'item.add', itemId: rows[index].itemId, quantity: value }]);
          });
        });
      }
    });
  }

  // ------------------------------------------------------------ 角色与卡片

  function openCharaPicker() {
    openPicker({
      title: '添加魔法少女',
      placeholder: '搜索角色名称（已实装 231 名）',
      load: function (q) { return api('/api/view/chara-catalog?limit=120&q=' + encodeURIComponent(q)); },
      meta: function (data) { return '匹配 ' + data.total + ' 名'; },
      render: function (rows) {
        var html = '<div class="grid wide">';
        rows.forEach(function (row) {
          html += '<div class="tile">' + thumb(row.face || row.icon, row.name, 'faceAvatar')
            + '<div class="name">' + esc(row.name) + '</div>'
            + '<div class="id">#' + esc(row.charaId) + ' · 初始 ' + esc(row.defaultCard ? row.defaultCard.rank : '无卡')
            + '</div>'
            + '<div class="tags"><span class="tag">' + esc(row.attributeLabel) + '</span>'
            + (row.owned ? '<span class="tag ok">已拥有</span>' : '<span class="tag">未拥有</span>') + '</div>'
            + (row.addable
              ? '<div class="row"><input data-lv type="number" min="1" value="1" style="width:70px">'
                + '<button class="mini" data-add-chara="' + esc(row.charaId) + '">获取</button></div>'
              : '<div class="row"><span class="small">' + (row.owned ? '已持有该角色' : '缺少卡片数据') + '</span></div>')
            + '</div>';
        });
        return html + '</div>';
      },
      bind: function (host, rows) {
        host.querySelectorAll('[data-add-chara]').forEach(function (button) {
          button.addEventListener('click', function (event) {
            var tile = button.closest('.tile');
            var level = readInteger(tile.querySelector('[data-lv]'));
            if (level === null) { event.preventDefault(); return; }
            edit([{ op: 'chara.add', charaId: Number(button.getAttribute('data-add-chara')), level: level }]);
          });
        });
      }
    });
  }

  function renderCharas() {
    if (S.charaSel) { return renderCharaDetail(); }
    var tabId = 'charas';
    var page = pageOf(tabId);
    var limit = 120;
    var url = '/api/view/charas?offset=' + page.offset + '&limit=' + limit + '&q=' + encodeURIComponent(page.q || '');
    return paintCurrent(api(url), function (data) {
      var html = '<div class="toolbar"><input type="text" data-search placeholder="搜索角色名称或卡片名">'
        + '<button data-add-chara>添加魔法少女…</button>'
        + '<span class="spacer"></span><span class="small">共 ' + data.total
        + ' 名魔法少女（点击卡片进入详细编辑）</span></div><div class="grid wide">';
      data.rows.forEach(function (row) {
        html += '<div class="tile cat" data-chara="' + esc(row.userCardId) + '">'
          + thumb(row.face || row.icon, row.name, 'faceAvatar')
          + '<div class="name">' + esc(row.name) + '</div>'
          + '<div class="id">' + esc(row.cardName) + '</div>'
          + '<div class="tags"><span class="tag">' + esc(row.attributeLabel) + '</span>'
          + '<span class="tag">' + esc(row.cardRank || '') + '</span>'
          + '<span class="tag ok">Lv ' + esc(row.level) + '</span></div>'
          + statLine([['Magia', row.magiaLevel], ['命运宝石', row.lbItemNum]])
          + '</div>';
      });
      html += '</div>' + pagerHtml(data.total, limit, page);
      $('main').innerHTML = html;
      searchBox(tabId, '搜索角色', render);
      bindPager(tabId, limit, render);
      $('main').querySelector('[data-add-chara]').addEventListener('click', openCharaPicker);
      $('main').querySelectorAll('[data-chara]').forEach(function (tile) {
        tile.addEventListener('click', function () {
          S.charaSel = tile.getAttribute('data-chara');
          render();
        });
      });
    });
  }

  function charaRow(label, value, spec, options) {
    var html = '<div class="fieldRow" data-spec="' + esc(JSON.stringify(spec || {})) + '">';
    html += '<label>' + esc(label) + '</label>';
    if (options) {
      html += '<select data-field>';
      options.forEach(function (item) {
        var selected = String(item[0]) === String(value) ? ' selected' : '';
        html += '<option value="' + esc(item[0]) + '"' + selected + '>' + esc(item[1]) + '</option>';
      });
      html += '</select>';
    } else if (typeof value === 'boolean') {
      html += '<select data-field><option value="1"' + (value ? ' selected' : '') + '>是</option>'
        + '<option value="0"' + (value ? '' : ' selected') + '>否</option></select>';
    } else {
      var shown = (value === null || value === undefined) ? '' : value;
      html += '<input data-field type="text" value="' + esc(shown) + '">';
    }
    html += '<button class="mini" data-apply>应用</button></div>';
    return html;
  }


  function charaPanelList() {
    return [
      { key: 'level', label: '等级与经验', desc: '升级 / 降级，数值按公式重算' },
      { key: 'magia', label: '魔力（Magia）', desc: 'Magia 等级 1-5' },
      { key: 'revision', label: '魔力解放', desc: '已解放次数与命运宝石（每次 +1 结晶槽位）' },
      { key: 'episode', label: '剧情 Episode', desc: '羁绊点与 Episode 等级' },
      { key: 'spirit', label: '精神强化', desc: 'Spirit 树（含情感技能）开启 / 关闭' },
      { key: 'attribute', label: '属性强化', desc: '对同属性全体角色生效的 HP / 攻 / 防' },
      { key: 'awake', label: '觉醒（升星）', desc: '升星链、素材与觉醒费用' },
      { key: 'story', label: '剧情（角色 / 衣装）', desc: '角色剧情与衣装剧情的通关状态' },
      { key: 'costume', label: '衣装', desc: '衣装获取状态与当前穿着' },
      { key: 'skin', label: '外观', desc: '卡面、Live2D、启用状态' },
      { key: 'rank', label: '星级切换', desc: '换成同角色的任意星级卡' }
    ];
  }

  function charaHeader(d, card) {
    return '<section class="panel charaHead">' + thumb(d.face || d.icon, d.name, 'charaFace')
      + '<div><h2>' + esc(d.name) + ' <span class="small">' + esc(d.kana || '') + '</span></h2>'
      + '<p class="small">' + esc(d.attributeLabel) + ' · ' + esc(d.school || '') + ' · '
      + esc(card.cardName || '') + ' #' + esc(card.cardId || '') + '</p>'
      + '<div class="tags"><span class="tag">' + esc(card.rank || '') + '</span>'
      + '<span class="tag ok">Lv ' + esc(card.level) + ' / ' + esc(card.maxLevel || '?') + '</span>'
      + '<span class="tag">Magia ' + esc(card.magiaLevel) + '</span>'
      + '<span class="tag">Episode ' + esc(d.episode.level) + ' / 5</span>'
      + '<span class="tag">命运宝石 ' + esc(d.chara.lbItemNum || 0) + '</span></div></div></section>';
  }

  function charaPanelHtml(panel, d, card) {
    if (panel === 'level') {
      return '<section class="panel"><h2>等级与经验 <span class="tag ok">可回退</span></h2>'
        + charaRow('等级（上限 ' + esc(card.maxLevel || '?') + '）', card.level,
          { op: 'card.set', id: d.userCardId, field: 'level' })
        + charaRow('经验（本级需要 ' + esc(card.experienceCap || 0) + '）', card.experience,
          { op: 'card.set', id: d.userCardId, field: 'experience' })
        + '<p class="small">改等级后 HP / 攻击 / 防御按游戏公式重算，经验清零。</p></section>';
    }
if (panel === 'magia') {
      var dop = d.doppel || {};
      var html = '<section class="panel"><h2>魔力（Magia） <span class="tag ok">可回退</span></h2>'
        + charaRow('Magia 等级', card.magiaLevel,
          { op: 'card.set', id: d.userCardId, field: 'magiaLevel' },
          [[1, '1'], [2, '2'], [3, '3'], [4, '4'], [5, '5']])
        + '<p class="small">游戏内需要素材与 Episode 等级；编辑器允许直接调整与回退。</p>';
      if (dop.doppelId) {
        html += '<h3 class="sub">魔女化身（Doppel）</h3><div class="kvList">'
          + '<div class="kv"><span>名称</span><strong>' + esc(dop.name || ('Doppel ' + dop.doppelId)) + '</strong></div>'
          + '<div class="kv"><span>称号</span><strong>' + esc(dop.title || '—') + '</strong></div>'
          + '<div class="kv"><span>Doppel ID</span><strong>' + esc(dop.doppelId) + '</strong></div>'
          + '<div class="kv"><span>状态</span><strong>' + (dop.unlocked ? '已解锁' : '未解锁') + '</strong></div>'
          + '</div><div class="row">'
          + (dop.unlocked
            ? '<button class="mini" data-doppel-set="0">取消解锁</button>'
            : '<button class="mini" data-doppel-set="1">解锁魔女化身</button>')
          + '<span class="small">游戏内要 Magia Lv5 并通关对应剧情；这里直接改存档里的 userDoppelList，'
          + '不改卡片、不改素材。</span></div>';
        if (dop.description) {
          html += '<details class="note"><summary class="small">简介</summary>'
            + '<p class="small pieceDesc">' + esc(nl(dop.description)) + '</p></details>';
        }
      } else {
        html += '<p class="small">官方 master 里没有这个角色的魔女化身。</p>';
      }
      return html + '</section>';
    }
    if (panel === 'revision') {
      var cap = 1 + (card.revision || 0);
      return '<section class="panel"><h2>魔力解放（revision） <span class="tag ok">可回退</span></h2>'
        + charaRow('已解放次数（revision）', card.revision,
          { op: 'card.set', id: d.userCardId, field: 'revision' },
          [[0, '0'], [1, '1'], [2, '2'], [3, '3']])
        + charaRow('命运宝石（lbItemNum）', d.chara.lbItemNum,
          { op: 'chara.set', userCardId: d.userCardId, field: 'lbItemNum' })
        + '<p class="small">每次魔力解放消耗命运宝石，记忆结晶槽位 = 1 + 已解放次数，最多 4 位'
        + '（当前 ' + esc(Math.min(4, cap)) + ' 位）。图标：' + thumb(d.gemIcon, '命运宝石', 'gemIcon') + '</p></section>';
    }
    if (panel === 'episode') {
      return '<section class="panel"><h2>剧情（Episode） <span class="tag ok">可回退</span></h2>'
        + charaRow('羁绊点（bondsTotalPt）', d.chara.bondsTotalPt,
          { op: 'chara.set', userCardId: d.userCardId, field: 'bondsTotalPt' })
        + '<p class="small">当前 Episode ' + esc(d.episode.level) + ' / 5'
        + (d.episode.next ? '，下一级需要 ' + esc(d.episode.next) + ' 羁绊点' : '，已满级')
        + '；阈值 ' + esc((d.episode.thresholds || []).join(' / ')) + '</p></section>';
    }

    if (panel === 'skin') {
      return '<section class="panel"><h2>外观</h2>'
        + charaRow('显示卡面 ID', card.displayCardId,
          { op: 'card.set', id: d.userCardId, field: 'displayCardId' })
        + charaRow('Live2D 服装 ID', d.chara.live2dId,
          { op: 'chara.set', userCardId: d.userCardId, field: 'live2dId' })
        + charaRow('启用中', !!card.enabled, { op: 'card.set', id: d.userCardId, field: 'enabled' })
        + '</section>';
    }
    if (panel === 'rank') {
      var html = '<section class="panel"><h2>星级切换（升星降星都允许）</h2>'
        + '<table><thead><tr><th>星级</th><th>卡片</th><th>操作</th></tr></thead><tbody>';
      (d.siblings || []).forEach(function (row) {
        var current = String(row.cardId) === String(card.cardId);
        html += '<tr><td>' + esc(row.rank) + '</td><td>' + esc(row.name) + ' <span class="small">#'
          + esc(row.cardId) + '</span></td><td>'
          + (current ? '<span class="tag ok">当前使用</span>'
            : '<button class="mini" data-swap-card="' + esc(row.cardId) + '">换成这张卡</button>')
          + '</td></tr>';
      });
      return html + '</tbody></table></section>';
    }
    if (panel === 'spirit') { return spiritPanelHtml(d); }
    if (panel === 'attribute') { return attributePanelHtml(d); }
    if (panel === 'awake') { return awakePanelHtml(d); }
    if (panel === 'story') { return storyPanelHtml(d); }
    if (panel === 'costume') { return costumePanelHtml(d); }
    return '';
  }

// ------------------------------------------------------------ 精神强化（SVG 树）

  // 节点贴图取自 APK 内 image_web/page/chara/tree/icon_*.png（70×70，游戏原图）。
  var SPIRIT_ICON = {
    HP: 'icon_hp.png', ATTACK: 'icon_atk.png', DEFENSE: 'icon_def.png',
    DISK_ACCELE: 'icon_accele.png', DISK_BLAST: 'icon_blast.png', DISK_CHARGE: 'icon_charge.png'
  };
  var SPIRIT_FIELD_NAME = { HP: 'HP', ATTACK: '攻击', DEFENSE: '防御', DISK_ACCELE: 'Accele 加成',
    DISK_BLAST: 'Blast 加成', DISK_CHARGE: 'Charge 加成' };

  function spiritIcon(node) {
    if (node.skill) {
      return '/assets/spirit/' + (node.skill.type === 'ABILITY' ? 'icon_ability.png' : 'icon_skill.png');
    }
    var file = SPIRIT_ICON[node.type];
    return file ? '/assets/spirit/' + file : '';
  }

  function spiritCapLabel(node) {
    if (!node) { return ''; }
    if (node.type === 'START') { return '起点'; }
    if (node.skill) { return node.skill.type === 'ABILITY' ? '能力' : '技能'; }
    return '+' + (node.value || 0);
  }

  function spiritLabel(node) {
    if (!node) { return ''; }
    if (node.type === 'START') { return '起点'; }
    if (node.skill) { return node.skill.name || ('情感技能 ' + node.skill.id); }
    return (SPIRIT_FIELD_NAME[node.type] || node.type) + ' +' + (node.value || 0);
  }

  function spiritTreeSvg(spirit, selectedId) {
    var nodes = spirit.nodes || [];
    var byId = {};
    nodes.forEach(function (node) { byId[String(node.cellId)] = node; });
    var b = spirit.bounds || { minX: 0, maxX: 4096, minY: 0, maxY: 4096 };
    var pad = 170;
    var w = Math.max(b.maxX - b.minX, 1) + pad * 2;
    var h = Math.max(b.maxY - b.minY, 1) + pad * 2;
    var svg = '<svg class="spiritTree" viewBox="' + (b.minX - pad) + ' ' + (b.minY - pad) + ' ' + w + ' ' + h
      + '" preserveAspectRatio="xMidYMid meet"><g>';
    var seen = {};
    nodes.forEach(function (node) {
      (node.links || []).forEach(function (childId) {
        var child = byId[String(childId)];
        if (!child) { return; }
        var key = Math.min(node.cellId, childId) + ':' + Math.max(node.cellId, childId);
        if (seen[key]) { return; }
        seen[key] = true;
        svg += '<line class="spiritLink' + (node.opened && child.opened ? ' on' : '') + '" x1="' + node.x
          + '" y1="' + node.y + '" x2="' + child.x + '" y2="' + child.y + '"/>';
      });
    });
    nodes.forEach(function (node) {
      var cls = 'spiritNode type-' + String(node.type || '').toLowerCase().replace(/_/g, '-');
      if (node.opened) { cls += ' opened'; }
      if (node.canOpen) { cls += ' can'; }
      if (String(node.cellId) === String(selectedId)) { cls += ' sel'; }
      var icon = spiritIcon(node);
      svg += '<g class="' + cls + '" data-spirit-node="' + esc(node.cellId) + '" transform="translate(' + node.x
        + ',' + node.y + ')"><circle r="48"/>' + (icon
          ? '<image href="' + icon + '" xlink:href="' + icon
            + '" x="-35" y="-35" width="70" height="70" preserveAspectRatio="xMidYMid meet"/>'
          : '<text class="glyph" y="18">★</text>')
        + '<text class="cap" y="92">' + esc(spiritCapLabel(node)) + '</text></g>';
    });
    return svg + '</g></svg>';
  }

  function spiritSideHtml(node, spirit) {
    if (!node) { return '<div class="spiritSide"><p class="small">点击左侧节点查看详情。</p></div>'; }
    var html = '<div class="spiritSide"><h3>' + esc(spiritLabel(node)) + '</h3><div class="kvList">'
      + '<div class="kv"><span>节点 ID</span><strong>' + esc(node.cellId) + '</strong></div>'
      + '<div class="kv"><span>类型</span><strong>' + esc(node.type) + '</strong></div>';
    if (node.field) {
      html += '<div class="kv"><span>加成</span><strong>+' + esc(node.value) + ' ' + esc(node.field) + '</strong></div>';
    }
    html += '<div class="kv"><span>前置</span><strong>' + esc(node.need || '无') + '</strong></div>';
    if (node.skill) {
      html += '<div class="kv"><span>情感技能</span><strong>' + esc(node.skill.name || '') + '</strong></div>'
        + '<div class="kv"><span>说明</span><strong>' + esc(node.skill.description || '') + '</strong></div>';
    }
    html += '<div class="kv"><span>状态</span><strong>' + (node.opened ? '已开启' : (node.canOpen ? '可开启' : '锁定'))
      + '</strong></div></div>';
    if (node.cost && node.cost.length) {
      html += '<table class="miniTable"><thead><tr><th>游戏内需求</th><th>需要</th><th>持有</th></tr></thead><tbody>';
      node.cost.forEach(function (cost) {
        html += '<tr><td>' + esc(cost.name) + '</td><td class="num">' + esc(cost.need)
          + '</td><td class="num">' + esc(cost.have) + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    if (node.needCC) {
      html += '<p class="small">游戏内需 ' + esc(node.needCC) + ' 魔法石（当前持有 ' + esc(spirit.riche) + '）</p>';
    }
    html += '<p class="small">以上为游戏内需求，仅供参考：编辑器开启 / 关闭节点不消耗素材，也不扣魔法石。</p>';
    html += '<div class="row">';
    if (node.opened) {
      if (node.type !== 'START') {
        html += '<button class="mini" data-spirit-close="' + esc(node.cellId) + '">关闭该节点</button>';
      } else {
        html += '<span class="tag ok">精神树根节点</span>';
      }
    } else if (node.canOpen) {
      html += '<button class="mini" data-spirit-open="' + esc(node.cellId) + '">开启该节点</button>';
    } else {
      html += '<span class="tag">需先开启前置 ' + esc(node.need) + '</span>';
    }
    return html + '</div></div>';
  }

  function bindSpiritSide(d) {
    $('main').querySelectorAll('[data-spirit-open]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'spirit.open', charaId: d.charaId,
          cellId: Number(button.getAttribute('data-spirit-open')) }]);
      });
    });
    $('main').querySelectorAll('[data-spirit-close]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'spirit.close', charaId: d.charaId,
          cellId: Number(button.getAttribute('data-spirit-close')) }]);
      });
    });
  }

  function spiritPanelHtml(d) {
    var spirit = d.spirit || {};
    if (!spirit.treeAvailable) {
      return '<section class="panel"><h2>精神强化（Spirit）</h2><p class="small">'
        + esc(spirit.reason || '该角色没有可用的精神树节点') + '</p></section>';
    }
    var nodes = spirit.nodes || [];
    var selected = null;
    nodes.forEach(function (node) {
      if (String(node.cellId) === String(S.spiritSel)) { selected = node; }
    });
    if (!selected) {
      nodes.forEach(function (node) { if (!selected && node.opened) { selected = node; } });
      if (!selected) { selected = nodes[0]; }
    }
    var totals = spirit.totals || {};
    return '<section class="panel"><h2>精神强化（Spirit）<span class="spacer"></span>'
      + '<span class="small">组 ' + esc(spirit.groupId) + ' · 已开启 ' + esc(spirit.ownedCount) + ' / '
      + esc(spirit.cellCount) + ' · 情感技能 ' + esc(spirit.skillCount) + '</span></h2>'
      + '<div class="spiritWrap">' + spiritTreeSvg(spirit, selected && selected.cellId)
      + spiritSideHtml(selected, spirit) + '</div>'
      + '<div class="kvList"><div class="kv"><span>HP</span><strong>' + fmt(totals.addendHp) + '</strong></div>'
      + '<div class="kv"><span>攻击</span><strong>' + fmt(totals.addendAttack) + '</strong></div>'
      + '<div class="kv"><span>防御</span><strong>' + fmt(totals.addendDefense) + '</strong></div>'
      + '<div class="kv"><span>Accele / Blast / Charge</span><strong>'
      + fmt(totals.addendAccelBonus) + ' / ' + fmt(totals.addendBlastBonus) + ' / '
      + fmt(totals.addendChargeBonus) + '</strong></div></div>'
      + '<div class="row"><button class="mini" data-spirit-clear>清空全部（保留起点）</button>'
      + '<span class="small">开关直接生效：不扣素材、不扣魔法石，也不改持有数量。</span></div></section>';
  }

  // ------------------------------------------------------------ 属性强化

  function attributePanelHtml(d) {
    var a = d.attribute || {};
    var html = '<section class="panel"><h2>属性强化 <span class="tag ok">同属性全体生效</span>'
      + '<span class="spacer"></span><span class="small">' + esc(a.attributeLabel) + ' · 影响 '
      + esc(a.memberCount) + ' 名角色 · 已开启 ' + esc(a.openedCount) + ' / ' + esc(a.cellTotal)
      + ' · 卡片突破 ' + esc(a.cardRevision) + '</span></h2>';
    var stats = ['HP', 'ATTACK', 'DEFENSE'];
    var byStat = {};
    (a.cells || []).forEach(function (cell) {
      if (!byStat[cell.stat]) { byStat[cell.stat] = []; }
      byStat[cell.stat].push(cell);
    });
    html += '<table><thead><tr><th>项目</th><th>阶段</th><th>效果</th><th>需求</th><th>状态</th></tr></thead><tbody>';
    stats.forEach(function (stat) {
      (byStat[stat] || []).forEach(function (cell) {
        var costs = (cell.cost || []).map(function (cost) {
          return esc(cost.name) + ' ' + esc(cost.need) + '（持有 ' + esc(cost.have) + '）';
        }).join('<br>');
        html += '<tr><td>' + esc(cell.statLabel) + '</td><td class="num">突破 ' + esc(cell.revision) + '</td>'
          + '<td class="num">+' + esc(cell.value) + '</td><td class="small">' + costs + '</td><td>';
        if (cell.opened) {
          html += '<span class="tag ok">已开启</span> <button class="mini" data-attr-close="' + esc(cell.cellId)
            + '">关闭</button>';
        } else if (!cell.revisionOk) {
          html += '<span class="tag">需突破 ' + esc(cell.revision) + '</span>';
        } else {
          html += '<button class="mini" data-attr-open="' + esc(cell.cellId) + '">开启</button>';
        }
        html += '</td></tr>';
      });
    });
    html += '</tbody></table>';
    html += '<div class="row"><button class="mini" data-attr-open-all>开启当前突破可用的全部格</button>'
      + '<span class="small">写入 userCharaAtbEnhancementCellList；状态值累加到 userStatusList，总战力按同属性 '
      + esc(a.memberCount) + ' 人结算。需求素材只作展示，开启不消耗。</span></div>';
    html += '<h3 class="sub">受影响的同属性角色</h3><div class="grid wide tight">';
    (a.members || []).forEach(function (member) {
      html += '<div class="tile cat"><div class="row">' + thumb(member.icon, member.name, 'icon')
        + '<div><div class="name">' + esc(member.name) + '</div><div class="id">#' + esc(member.charaId)
        + '</div></div></div></div>';
    });
    return html + '</div></section>';
  }

  // ------------------------------------------------------------ 觉醒与突破

// ------------------------------------------------------------ 剧情（角色 / 衣装）

  function storyRewardText(reward) {
    if (!reward) { return ''; }
    return (reward.name || '—') + (reward.quantity ? ' ×' + reward.quantity : '');
  }

  function storyRowHtml(row, withCostume) {
    var quests = row.quests || [];
    var done = 0;
    quests.forEach(function (quest) { if (quest.cleared) { done += 1; } });
    var title = esc(row.title || ('段 ' + row.sectionId));
    if (withCostume && row.live2dId) {
      title += ' <span class="tag">衣装 ' + esc(row.live2dId)
        + (row.costumeName ? ' · ' + esc(row.costumeName) : '（衣装表未收录）') + '</span>';
    }
    return '<tr><td class="num">' + esc(row.sectionId) + '</td>'
      + '<td>' + title + (row.area ? '<div class="small">' + esc(row.area) + '</div>' : '') + '</td>'
      + '<td class="num">' + esc(row.ap === null || row.ap === undefined ? '—' : row.ap) + '</td>'
      + '<td class="small">' + esc(storyRewardText(row.reward) || '—') + '</td>'
      + '<td class="num">' + esc(done) + ' / ' + esc(quests.length) + '</td>'
      + '<td>' + (row.cleared ? '<span class="tag ok">已通关</span>' : '<span class="tag">未通关</span>') + '</td>'
      + '<td><button class="mini" data-story-set="' + esc(row.sectionId) + '" data-story-value="1">通关</button> '
      + '<button class="mini" data-story-set="' + esc(row.sectionId) + '" data-story-value="0">回退</button></td></tr>';
  }

  function storyTableHtml(rows, withCostume) {
    var head = '<table><thead><tr><th>段</th><th>标题</th><th>AP</th><th>首通奖励</th><th>任务</th><th>状态</th>'
      + '<th>操作</th></tr></thead><tbody>';
    return head + rows.map(function (row) { return storyRowHtml(row, withCostume); }).join('') + '</tbody></table>';
  }

  function storyPanelHtml(d) {
    var st = d.stories || {};
    var charaRows = st.chara || [];
    var costumeRows = st.costume || [];
    var html = '<section class="panel"><h2>角色剧情 <span class="spacer"></span><span class="small">已通关 '
      + esc(st.charaCleared || 0) + ' / ' + esc(charaRows.length) + '</span></h2>';
    html += charaRows.length ? storyTableHtml(charaRows, false)
      : '<p class="small">这个角色没有角色剧情段。</p>';
    html += '</section><section class="panel"><h2>衣装剧情 <span class="spacer"></span><span class="small">已通关 '
      + esc(st.costumeCleared || 0) + ' / ' + esc(costumeRows.length) + '</span></h2>';
    html += costumeRows.length ? storyTableHtml(costumeRows, true)
      : '<p class="small">这个角色没有衣装剧情段。</p>';
    html += '<p class="small">⚠️ 这里只改存档里的通关状态：不会发放首通奖励（道具 / 衣装 / 结晶 / 魔女化身 / 命运宝石都不会进入仓库），'
      + '也不会解锁关卡。「通关」会把整段内所有任务一起置为已通关（缺的行自动补出）；「回退」把任务与段都改回未通关，'
      + '存档里原本是 NONE（无此条件）的任务标记保持不动。</p></section>';
    return html;
  }

  function costumePanelHtml(d) {
    var cos = d.costumes || {};
    var rows = cos.rows || [];
    var html = '<section class="panel"><h2>衣装 <span class="spacer"></span><span class="small">已获取 '
      + esc(cos.ownedCount || 0) + ' / ' + esc(cos.total || 0) + ' · 当前穿着 ' + esc(cos.current || '—')
      + '</span></h2><div class="grid wide">';
    rows.forEach(function (row) {
      html += '<div class="tile cat' + (row.current ? ' current' : '') + '">'
        + '<div class="name">' + esc(row.name || ('衣装 ' + row.live2dId)) + '</div>'
        + '<div class="id">live2dId ' + esc(row.live2dId) + '</div><div class="tags">'
        + (row.current ? '<span class="tag ok">当前穿着</span>' : '')
        + (row.owned ? '<span class="tag ok">已获取</span>' : '<span class="tag">未获取</span>')
        + (row.invalidMaster ? '<span class="tag warn">主数据无此编号 · 会导致游戏读不出存档</span>' : '')
        + (row.default ? '<span class="tag warn">默认服装 · 不可取消</span>' : '')
        + '</div><div class="row">';
      if (row.owned) {
        if (!row.current) {
          html += '<button class="mini" data-costume-equip="' + esc(row.live2dId) + '">设为当前</button>';
        }
        if (!row.default) {
          html += '<button class="mini" data-costume-remove="' + esc(row.live2dId) + '">取消获取</button>';
        }
      } else {
        html += '<button class="mini" data-costume-add="' + esc(row.live2dId) + '">获取</button>';
      }
      html += '</div></div>';
    });
    if (!rows.length) { html += '<p class="small">官方衣装表里没有这个角色的衣装。</p>'; }
    html += '</div><p class="small">默认服装（live2dId 00）永远保持已获取，也不能取消 —— 战斗与剧情都依赖它。'
      + '正在穿着的衣装要先换成别的才能取消获取。</p>'
      + '<div class="row"><button class="mini" data-costume-clean>清理主数据里不存在的衣装</button>'
      + '<span class="small">别人给的存档进不去游戏时，先点这个（会删掉 live2dId 在 MCATBIN1 里查不到的行）</span></div>'
      + '</section>';
    return html;
  }

  function awakePanelHtml(d) {
    var awake = d.awakening || {};
    var card = awake.card || {};
    var evolve = awake.evolve || {};
    var html = '<section class="panel"><h2>觉醒（升星）'
      + '<span class="spacer"></span><span class="small">' + esc(card.rank) + ' → '
      + esc(evolve.nextRank || '已满星') + '</span></h2>';
    html += '<div class="kvList"><div class="kv"><span>当前卡片</span><strong>' + esc(card.name) + ' #'
      + esc(card.cardId) + '</strong></div><div class="kv"><span>下一张</span><strong>'
      + esc(evolve.nextName || '—') + (evolve.nextCard ? ' #' + esc(evolve.nextCard) : '') + '</strong></div>'
      + '<div class="kv"><span>等级</span><strong>' + esc(card.level) + ' / ' + esc(card.maxLevel) + ' '
      + (evolve.levelOk ? '✔' : '（需满级）') + '</strong></div>'
      + '<div class="kv"><span>觉醒费用</span><strong>' + esc(evolve.cost) + ' CC</strong></div>'
      + '<div class="kv"><span>觉醒链</span><strong>' + esc((evolve.chain || []).join(' → ')) + '</strong></div></div>';
    html += '<table class="miniTable"><thead><tr><th>位置</th><th>素材</th><th>需要</th><th>持有</th><th>加成</th>'
      + '<th>状态</th></tr></thead><tbody>';
    (evolve.slots || []).forEach(function (slot) {
      html += '<tr class="' + (slot.done ? 'doneRow' : '') + '"><td class="num">' + esc(slot.target) + '</td>'
        + '<td>' + (slot.icon ? thumb(slot.icon, slot.giftName, 'giftIcon') : '')
        + esc(slot.giftName || ('gift ' + slot.giftId)) + '</td><td class="num">' + esc(slot.quantity) + '</td>'
        + '<td class="num">' + esc(slot.have) + '</td>'
        + '<td class="num">+' + esc(slot.bonusNum) + ' ' + esc(slot.bonusCode) + '</td><td>'
        + (slot.done ? '<span class="tag ok">已填充</span> <button class="mini" data-awake-slot="' + esc(slot.target)
          + '" data-slot-value="0">取消</button>'
          : '<button class="mini" data-awake-slot="' + esc(slot.target) + '" data-slot-value="1">填充</button>')
        + '</td></tr>';
    });
    html += '</tbody></table>'
      + '<div class="row"><button class="mini" data-awake-slot-all="1">填充全部位置</button>'
      + '<button class="mini" data-awake-slot-all="0">清空全部位置</button>'
      + '<span class="small">填充只写 customized1..6 与总战力（每槽 +' + esc(evolve.forcePerSlot)
      + '），不动 gift 持有数；填充后仍可在游戏内自行点觉醒，或用「星级切换」直接换到高星卡片。</span></div>';
    return html + '</section>';
  }

function renderCharaDetail() {
    var panel = S.charaPanel;
    return paintCurrent(api('/api/chara?userCardId=' + encodeURIComponent(S.charaSel)), function (d) {
      var extra = null;
      if (panel === 'attribute') {
        extra = api('/api/view/attribute?charaId=' + encodeURIComponent(d.charaId)).then(function (data) {
          d.attribute = data;
        });
      } else if (panel === 'awake') {
        extra = api('/api/view/awakening?userCardId=' + encodeURIComponent(d.userCardId)).then(function (data) {
          d.awakening = data;
        });
      }
      return paintCurrent(extra || Promise.resolve(), function () { paintCharaDetail(d, panel); });
    });
  }

  function paintCharaDetail(d, panel) {
    var card = d.card || {};
    var html = '<div class="crumbs"><button class="mini" data-back>魔法少女一览</button>'
      + '<span>›</span><button class="mini" data-home>' + esc(d.name) + '</button>'
      + (panel ? '<span>›</span><span class="small">' + esc((charaPanelList().filter(function (item) {
        return item.key === panel;
      })[0] || {}).label || '') + '</span>' : '')
      + '</div>' + charaHeader(d, card);

    if (!panel) {
      html += '<div class="grid wide">';
      charaPanelList().forEach(function (item) {
        html += '<div class="tile cat" data-panel="' + item.key + '">'
          + '<div class="name">' + esc(item.label) + '</div>'
          + '<div class="id">' + esc(item.desc) + '</div></div>';
      });
      html += '</div>';
      html += '<section class="panel"><h2>只读数值（游戏推算）</h2><div class="kvList">'
        + '<div class="kv"><span>HP</span><strong>' + fmt(card.hp) + '</strong></div>'
        + '<div class="kv"><span>攻击力</span><strong>' + fmt(card.attack) + '</strong></div>'
        + '<div class="kv"><span>防御力</span><strong>' + fmt(card.defense) + '</strong></div>'
        + '<div class="kv"><span>属性强化（攻 / 防 / HP）</span><strong>'
        + fmt(d.charaReadonly.addendAttack) + ' / ' + fmt(d.charaReadonly.addendDefense) + ' / '
        + fmt(d.charaReadonly.addendHp) + '</strong></div>'
        + '<div class="kv"><span>Accele / Blast / Charge</span><strong>'
        + fmt(d.charaReadonly.addendAccelBonus) + ' / ' + fmt(d.charaReadonly.addendBlastBonus) + ' / '
        + fmt(d.charaReadonly.addendChargeBonus) + '</strong></div></div></section>';
    } else {
      html += charaPanelHtml(panel, d, card);
    }

    $('main').innerHTML = html;
    var back = $('main').querySelector('[data-back]');
    if (back) {
      back.addEventListener('click', function () {
        S.charaSel = null;
        S.charaPanel = null;
        S.spiritSel = null;
        render();
      });
    }
    var home = $('main').querySelector('[data-home]');
    if (home) {
      home.addEventListener('click', function () {
        S.charaPanel = null;
        S.spiritSel = null;
        render();
      });
    }
    $('main').querySelectorAll('[data-panel]').forEach(function (tile) {
      tile.addEventListener('click', function () {
        S.charaPanel = tile.getAttribute('data-panel');
        S.spiritSel = null;
        render();
      });
    });
    $('main').querySelectorAll('[data-apply]').forEach(function (button) {
      button.addEventListener('click', function () {
        var row = button.closest('[data-spec]');
        var spec = JSON.parse(row.getAttribute('data-spec'));
        var input = row.querySelector('[data-field]');
        var raw = input.value;
        var value;
        if (spec.field === 'enabled') {
          value = raw === '1';
        } else if (spec.field === 'live2dId') {
          value = raw;
        } else {
          value = spec.field === 'level' ? readInteger(input, 1, card.maxLevel || null) : readInteger(input);
          if (value === null) { return; }
        }
        var op = { op: spec.op, field: spec.field, value: value };
        if (spec.op === 'card.set') { op.id = spec.id; } else { op.userCardId = spec.userCardId; }
        edit([op]);
      });
    });
    $('main').querySelectorAll('[data-swap-card]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'card.swap', userCardId: d.userCardId,
          cardId: Number(button.getAttribute('data-swap-card')) }]);
      });
    });
    bindSpiritSide(d);
    $('main').querySelectorAll('[data-spirit-node]').forEach(function (node) {
      node.addEventListener('click', function () {
        var cellId = node.getAttribute('data-spirit-node');
        S.spiritSel = cellId;
        // 只换选中态与右侧详情，不整页重绘：滚动位置与树的位置都不动。
        var tree = $('main').querySelector('svg.spiritTree');
        if (tree) {
          tree.querySelectorAll('.spiritNode.sel').forEach(function (item) {
            item.classList.remove('sel');
          });
          node.classList.add('sel');
        }
        var spirit = d.spirit || {};
        var picked = null;
        (spirit.nodes || []).forEach(function (item) {
          if (String(item.cellId) === String(cellId)) { picked = item; }
        });
        var side = $('main').querySelector('.spiritSide');
        if (side) {
          side.outerHTML = spiritSideHtml(picked, spirit);
          bindSpiritSide(d);
        }
      });
    });
    var clearSpirit = $('main').querySelector('[data-spirit-clear]');
    if (clearSpirit) {
      clearSpirit.addEventListener('click', function () {
        if (!window.confirm('关闭这个角色的全部精神节点（保留起点）？')) { return; }
        edit([{ op: 'spirit.close', charaId: d.charaId, all: true }]);
      });
    }
    $('main').querySelectorAll('[data-attr-open]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'attribute.open', charaId: d.charaId,
          cellId: Number(button.getAttribute('data-attr-open')) }]);
      });
    });
    $('main').querySelectorAll('[data-attr-close]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'attribute.close', charaId: d.charaId,
          cellId: Number(button.getAttribute('data-attr-close')) }]);
      });
    });
    var attrAll = $('main').querySelector('[data-attr-open-all]');
    if (attrAll) {
      attrAll.addEventListener('click', function () {
        edit([{ op: 'attribute.open', charaId: d.charaId, all: true }]);
      });
    }
    $('main').querySelectorAll('[data-doppel-set]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'doppel.set', charaId: d.charaId,
          unlocked: button.getAttribute('data-doppel-set') === '1' }]);
      });
    });
    var costumeClean = $('main').querySelector('[data-costume-clean]');
    if (costumeClean) {
      costumeClean.addEventListener('click', function () {
        edit([{ op: 'costume.clean' }]);
      });
    }
    $('main').querySelectorAll('[data-costume-add]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'costume.add', charaId: d.charaId,
          live2dId: button.getAttribute('data-costume-add') }]);
      });
    });
    $('main').querySelectorAll('[data-costume-remove]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'costume.remove', charaId: d.charaId,
          live2dId: button.getAttribute('data-costume-remove') }]);
      });
    });
    $('main').querySelectorAll('[data-costume-equip]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'costume.equip', charaId: d.charaId,
          live2dId: button.getAttribute('data-costume-equip') }]);
      });
    });
    $('main').querySelectorAll('[data-story-set]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'story.set', charaId: d.charaId,
          sectionId: Number(button.getAttribute('data-story-set')),
          cleared: button.getAttribute('data-story-value') === '1' }]);
      });
    });
    $('main').querySelectorAll('[data-awake-slot]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'awake.slot', userCardId: d.userCardId,
          targets: [Number(button.getAttribute('data-awake-slot'))],
          value: button.getAttribute('data-slot-value') === '1' }]);
      });
    });
    $('main').querySelectorAll('[data-awake-slot-all]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'awake.slot', userCardId: d.userCardId,
          value: button.getAttribute('data-awake-slot-all') === '1' }]);
      });
    });
  }

  // ------------------------------------------------------------ 记忆结晶

  var PIECE_TYPE_LABEL = { ABILITY: '能力', SKILL: '技能' };

  function pieceRankLabel(rank) {
    var text = String(rank || '');
    var match = text.match(/RANK_(\d)/);
    return match ? ('\u2605' + match[1]) : text;
  }

  function pieceTypeLabel(type) {
    return PIECE_TYPE_LABEL[type] || type || '';
  }

  function selectOptions(values, current, labelFn) {
    var html = '<option value="">全部</option>';
    (values || []).forEach(function (value) {
      html += '<option value="' + esc(value) + '"' + (String(value) === String(current) ? ' selected' : '') + '>'
        + esc(labelFn ? labelFn(value) : value) + '</option>';
    });
    return html;
  }

  function deckTagHtml(slot) {
    return '<span class="tag">' + esc(slot.name) + ' · 第' + esc(slot.member) + '位 · 槽' + esc(slot.slot) + '</span>';
  }

  function renderPieces() {
    if (S.pieceSel) { return renderPieceDetail(); }
    var tabId = 'pieces';
    var page = pageOf(tabId);
    var limit = 200;
    var filter = S.pieceFilter;
    var url = '/api/view/pieces?offset=' + page.offset + '&limit=' + limit
      + '&q=' + encodeURIComponent(page.q || '')
      + '&rank=' + encodeURIComponent(filter.rank || '')
      + '&type=' + encodeURIComponent(filter.type || '')
      + '&limitMode=' + encodeURIComponent(filter.limit || '')
      + '&charaId=' + encodeURIComponent(filter.charaId || '');
    var charaPromise = api('/api/view/charas?limit=200').catch(function () { return { rows: [] }; });
    return paintCurrent(Promise.all([api(url), charaPromise]), function (results) {
      var data = results[0];
      var charaRows = (results[1] && results[1].rows) || [];
      var html = '<div class="toolbar"><input type="text" data-search placeholder="搜索结晶名称或 pieceId">'
        + '<select data-rank-filter>' + selectOptions(data.ranks, filter.rank) + '</select>'
        + '<select data-type-filter>' + selectOptions(data.types, filter.type, pieceTypeLabel) + '</select>'
        + '<select data-limit-filter>'
        + '<option value="">限制：全部</option>'
        + '<option value="limited"' + (filter.limit === 'limited' ? ' selected' : '') + '>限制：仅限指定角色</option>'
        + '<option value="free"' + (filter.limit === 'free' ? ' selected' : '') + '>限制：通用</option>'
        + '</select>'
        + '<select data-chara-filter><option value="">角色：全部</option>'
        + charaRows.map(function (row) {
            return '<option value="' + esc(row.charaId) + '"'
              + (String(row.charaId) === String(filter.charaId) ? ' selected' : '') + '>'
              + esc(row.name) + '</option>';
          }).join('')
        + '</select>'
        + '<button data-add>添加记忆结晶…</button>'
        + '<button data-all-piece>全部等级设为…</button><span class="spacer"></span>'
        + '<span class="small">共 ' + data.total + ' 枚（同种可有多枚）· 点卡片看详情</span></div>';
      html += '<div class="grid wide">';
      data.rows.forEach(function (row) {
        html += '<div class="tile cat pieceTile" data-piece="' + esc(row.id) + '">'
          + thumb(row.icon, row.name, 'icon')
          + '<div class="name">' + esc(row.name) + '</div>'
          + '<div class="id">#' + esc(row.pieceId) + ' · ' + esc(pieceTypeLabel(row.type)) + ' · ' + esc(row.rank || '') + '</div>'
          + ((row.limitCharaNames && row.limitCharaNames.length) || (row.decks && row.decks.length)
            ? '<div class="tags">'
              + (row.limitCharaNames || []).map(function (name) {
                  return '<span class="tag warn">仅限 ' + esc(name) + '</span>';
                }).join('')
              + (row.decks || []).map(deckTagHtml).join('') + '</div>'
            : '')
          + '<div class="row"><span class="tag">Lv ' + esc(row.level) + ' / ' + esc(row.maxLevel || '?') + '</span>'
          + '<span class="tag">突破 ' + esc(row.lbCount) + ' / 4</span></div>'
          + statLine([['HP', row.hp]])
          + statLine([['攻击', row.attack]])
          + statLine([['防御', row.defense]])
          + '</div>';
      });
      html += '</div>' + pagerHtml(data.total, limit, page);
      $('main').innerHTML = html;
      searchBox(tabId, '搜索结晶', render);
      bindPager(tabId, limit, render);
      var rankSelect = $('main').querySelector('[data-rank-filter]');
      if (rankSelect) {
        rankSelect.addEventListener('change', function () {
          filter.rank = rankSelect.value;
          page.offset = 0;
          render();
        });
      }
      var typeSelect = $('main').querySelector('[data-type-filter]');
      if (typeSelect) {
        typeSelect.addEventListener('change', function () {
          filter.type = typeSelect.value;
          page.offset = 0;
          render();
        });
      }
      var limitSelect = $('main').querySelector('[data-limit-filter]');
      if (limitSelect) {
        limitSelect.addEventListener('change', function () {
          filter.limit = limitSelect.value;
          page.offset = 0;
          render();
        });
      }
      var charaSelect = $('main').querySelector('[data-chara-filter]');
      if (charaSelect) {
        charaSelect.addEventListener('change', function () {
          filter.charaId = charaSelect.value;
          page.offset = 0;
          render();
        });
      }
      $('main').querySelectorAll('[data-piece]').forEach(function (tile) {
        tile.addEventListener('click', function (event) {
          if (event.target.closest('input, button')) { return; }
          S.pieceSel = tile.getAttribute('data-piece');
          render();
        });
      });
      $('main').querySelector('[data-add]').addEventListener('click', openPiecePicker);
      var bulk = $('main').querySelector('[data-all-piece]');
      if (bulk) {
        bulk.addEventListener('click', function () {
          var value = window.prompt('把所有记忆结晶等级设为（超出上限按上限）', '30');
          if (value === null) { return; }
          var level = readInteger(value, 1, 50);
          if (level !== null) { edit([{ op: 'piece.setAllLevels', level: level }]); }
        });
      }
    });
  }

  function pieceFieldHtml(label, field, value, editable, max) {
    return '<div class="fieldRow"><label>' + esc(label) + '</label>'
      + '<input data-piece-field="' + esc(field) + '" type="number" min="' + (field === 'level' ? 1 : 0) + '"'
      + (max === undefined ? '' : ' max="' + esc(max) + '"')
      + ' value="' + esc(value) + '"' + (editable ? '' : ' disabled') + '>'
      + '<button class="mini" data-piece-apply="' + esc(field) + '"' + (editable ? '' : ' disabled') + '>应用</button></div>';
  }

  function renderPieceDetail() {
    return paintCurrent(api('/api/view/piece?id=' + encodeURIComponent(S.pieceSel)), function (d) {
      var html = '<div class="crumbs"><button class="mini" data-piece-back>记忆结晶一览</button>'
        + '<span>›</span><span class="small">' + esc(d.name) + '</span></div>';
      html += '<section class="panel charaHead">' + thumb(d.icon, d.name, 'charaFace')
        + '<div><h2>' + esc(d.name) + ' <span class="small">#' + esc(d.pieceId) + '</span></h2>'
        + '<p class="small">' + esc(d.rank || '') + ' · ' + esc(pieceTypeLabel(d.type))
        + (d.attribute ? ' · ' + esc(d.attribute) : '') + (d.kind ? ' · ' + esc(d.kind) : '') + '</p>'
        + '<div class="tags"><span class="tag">Lv ' + esc(d.level) + ' / ' + esc(d.maxLevel || '?') + '</span>'
        + '<span class="tag">突破 ' + esc(d.lbCount) + ' / 4</span>'
        + '<span class="tag ok">HP ' + fmt(d.hp) + '</span>'
        + '<span class="tag ok">攻 ' + fmt(d.attack) + '</span>'
        + '<span class="tag ok">防 ' + fmt(d.defense) + '</span>'
        + (d.limitCharaNames || []).map(function (name) {
            return '<span class="tag warn">仅限 ' + esc(name) + '</span>';
          }).join('')
        + '</div></div></section>';

      html += '<section class="panel"><h2>等级与突破 <span class="tag ok">可回退</span></h2>'
        + pieceFieldHtml('等级（上限 ' + (d.maxLevel || '?') + '）', 'level', d.level, d.editable, d.maxLevel || 50)
        + pieceFieldHtml('突破（0-4）', 'lbCount', d.lbCount, d.editable, 4)
        + '<p class="small">HP / 攻击 / 防御由等级与突破按游戏公式推算，不能直接改。</p>'
        + '<div class="row"><button class="mini" data-piece-remove>移出这枚结晶</button></div></section>';

      if ((d.skills || []).length) {
        html += '<section class="panel"><h2>技能</h2>';
        d.skills.forEach(function (skill) {
          var meta = [];
          if (skill.intervalTurn) { meta.push('冷却 ' + skill.intervalTurn + ' 回合'); }
          if (skill.unlockAt) { meta.push('满破（突破 ' + skill.unlockAt + '）后生效'); }
          if (skill.active === false) { meta.push('当前不生效'); }
          html += '<div class="skillRow' + (skill.active === false ? ' locked' : '') + '">'
            + '<div class="skillHead">' + esc(skill.name || ('技能 ' + skill.skillId))
            + (meta.length ? '<span class="small">（' + esc(meta.join('，')) + '）</span>' : '') + '</div>'
            + '<div class="small">' + esc(nl(skill.description)) + '</div></div>';
        });
        html += '</section>';
      }

      html += '<section class="panel"><h2>描述</h2><p class="pieceDesc">' + esc(nl(d.description) || '—') + '</p>'
        + (d.illustrator ? '<p class="small">画师：' + esc(d.illustrator) + '</p>' : '') + '</section>';

      html += '<section class="panel"><h2>编成到的队伍</h2>';
      if ((d.decks || []).length) {
        html += '<div class="kvList">';
        d.decks.forEach(function (slot) {
          html += '<div class="kv"><span>' + esc(slot.name) + '</span><strong>'
            + (slot.charaName ? esc(slot.charaName) + ' · ' : '')
            + '第 ' + esc(slot.member) + ' 位 · 槽 ' + esc(slot.slot) + '</strong></div>';
        });
        html += '</div>';
      } else {
        html += '<p class="small">这枚结晶目前没有编成在任何队伍里。</p>';
      }
      html += '</section>';

      $('main').innerHTML = html;
      var back = $('main').querySelector('[data-piece-back]');
      back.addEventListener('click', function () {
        S.pieceSel = null;
        render();
      });
      var removeButton = $('main').querySelector('[data-piece-remove]');
      if (removeButton) {
        removeButton.addEventListener('click', function () {
          if (!window.confirm('把这枚结晶移出存档？')) { return; }
          S.pieceSel = null;
          edit([{ op: 'piece.delete', id: d.id }]);
        });
      }
      $('main').querySelectorAll('[data-piece-apply]').forEach(function (button) {
        button.addEventListener('click', function () {
          var field = button.getAttribute('data-piece-apply');
          var input = $('main').querySelector('[data-piece-field="' + field + '"]');
          var value = readInteger(input);
          if (value === null) { return; }
          edit([{ op: 'piece.set', id: d.id, field: field, value: value }]);
        });
      });
    });
  }

  // 游戏的格子编号是「按列、从下往上」：
  //   3 6 9
  //   2 5 8
  //   1 4 7
  var POSITION_VISUAL_ORDER = [3, 6, 9, 2, 5, 8, 1, 4, 7];

  function positionGridHtml(position, helper) {
    var current = Number(position);
    var html = '<span class="posGrid' + (helper ? ' helper' : '')
      + '" title="九宫格位置 ' + esc(position || '') + '">';
    POSITION_VISUAL_ORDER.forEach(function (slot) {
      html += '<i class="' + (slot === current ? 'on' : '') + '"></i>';
    });
    return html + '</span>';
  }

  function starsHtml(count) {
    if (!count) { return ''; }
    var stars = '';
    for (var index = 0; index < count; index += 1) { stars += '★'; }
    return '<span class="stars" title="' + esc(count) + ' 星级">' + stars + '</span>';
  }

  function statChip(label, value, bonus) {
    return '<span class="statChip">' + esc(label) + ' <b>' + fmt(value) + '</b>'
      + (bonus ? ' <i class="plus">(+' + fmt(bonus) + ')</i>' : '') + '</span>';
  }

  function deckSlotHtml(deck, item, detailed) {
    var kind = item.kind || 'member';
    var slotText = (item.slot === null || item.slot === undefined) ? '' : item.slot;
    var html = '<div class="deckSlot' + (kind === 'helper' ? ' helperSlot' : '')
      + (kind === 'empty' ? ' emptySlot' : '') + '" data-member="' + esc(slotText) + '">';
    if (kind === 'helper') {
      html += '<div class="deckMember helper"><div class="memberText">'
        + '<div class="name">支援（好友）' + positionGridHtml(item.position, true) + '</div>'
        + '<div class="id">格 ' + esc(item.position) + ' · 好友填充</div></div></div>'
        + '<div class="deckPieces"><span class="small">这一格不占自己的角色与结晶</span></div>'
        + '<div class="row">'
        + (deck.editable ? '<button class="mini" data-deck-helper>调整支援位</button>'
          : '<span class="small">只读编成</span>')
        + '</div>';
      return html + '</div>';
    }
    if (kind === 'empty') {
      html += '<div class="deckMember empty">空位' + positionGridHtml(item.position, false) + '</div>'
        + '<div class="deckPieces"><span class="small">格 ' + esc(item.position) + ' 空着</span></div>'
        + '<div class="row">'
        + (deck.editable ? '<button class="mini" data-empty-position="' + esc(item.position) + '">放入角色</button>'
          : '<span class="small">只读编成</span>')
        + '</div>';
      return html + '</div>';
    }
    if (item.userCardId) {
      html += '<div class="deckMember row" data-goto-chara="' + esc(item.userCardId) + '">'
        + thumb(item.face || item.icon, item.name, 'faceIcon')
        + '<div class="memberText"><div class="name">' + esc(item.name) + starsHtml(item.rankStars)
        + positionGridHtml(item.position, false) + '</div>'
        + '<div class="id">Lv ' + esc(item.level) + ' · Magia ' + esc(item.magiaLevel)
        + ' · ' + (item.doppelUnlocked ? 'Doppel 已解锁' : 'Doppel 未解锁') + '</div>'
        + '<div class="stats">'
        + statChip('HP', item.total ? item.total.hp : 0, item.attributeBonus ? item.attributeBonus.hp : 0)
        + statChip('攻', item.total ? item.total.attack : 0, item.attributeBonus ? item.attributeBonus.attack : 0)
        + statChip('防', item.total ? item.total.defense : 0, item.attributeBonus ? item.attributeBonus.defense : 0)
        + '</div></div></div>';
    } else {
      html += '<div class="deckMember empty">空位</div>';
    }
    html += '<div class="deckPieces">';
    (item.pieces || []).forEach(function (piece) {
      html += '<div class="deckPiece" data-goto-piece="' + esc(piece.id) + '">'
        + thumb(piece.icon, piece.name, 'pieceMini')
        + '<span>' + esc(piece.name) + '</span>'
        + '<span class="small pieceLv">Lv' + esc(piece.level) + ' 突' + esc(piece.lbCount) + '</span>'
        + (deck.editable ? '<button class="mini" data-deck-piece-remove="1" data-slot="' + esc(piece.slot)
          + '">×</button>' : '') + '</div>';
    });
    if (!(item.pieces || []).length) {
      html += '<span class="small">无结晶</span>';
    }
    var capacity = item.pieceCapacity || 1;
    html += '<span class="small capacityHint">结晶 ' + (item.pieces || []).length + ' / '
      + capacity + '</span></div><div class="row">';
    if (deck.editable) {
      html += '<button class="mini" data-deck-member="' + esc(item.slot) + '">'
        + (item.userCardId ? '更换角色' : '放入角色') + '</button>';
      if (item.userCardId) {
        html += '<button class="mini" data-deck-member-remove="' + esc(item.slot) + '">移除</button>';
        if ((item.pieces || []).length < capacity) {
          html += '<button class="mini" data-deck-piece-add="' + esc(item.slot) + '">加结晶</button>';
        }
        html += ''
          + (detailed ? '<button class="mini" data-deck-swap="' + esc(item.slot) + '">换位…</button>' : '');
      }
    } else {
      html += '<span class="small">只读编成</span>';
    }
    return html + '</div></div>';
  }

  var DECK_KIND_LABEL = { quest: '任务编队', support: '支援编成', arena: '镜界队伍', stage: '舞台对战', other: '其他编成' };

  function deckBoardHtml(deck) {
    var cells = {};
    (deck.layout || []).forEach(function (item) {
      if (item.position) { cells[item.position] = item; }
    });
    var html = '<div class="deckBoard">';
    POSITION_VISUAL_ORDER.forEach(function (slot) {
      var item = cells[slot];
      var buff = (deck.formationSkills || {})[String(slot)];
      var cls = 'boardCell' + (item ? ' ' + item.kind : '') + (buff ? ' effective' : '');
      var inner = '<span class="pos">' + slot + '</span>';
      if (buff) {
        inner += '<img class="buffMark" src="' + esc(attrIconUrl(buff.attribute))
          + '" title="' + esc(buff.description || buff.name || '') + '" alt="' + esc(buff.attribute || '') + '">';
      }
      if (item && item.userCardId) {
        inner += '<img class="boardFace" loading="lazy" src="' + esc(item.face || item.icon)
          + '" alt="' + esc(item.name || '') + '">';
        inner += '<span class="who">' + esc(item.name || '') + '</span>';
      } else if (item && item.kind === 'helper') {
        inner += '<span class="whoCenter">支援</span>';
      } else {
        inner += '<span class="whoCenter empty">空位</span>';
      }
      html += '<div class="' + cls + '">' + inner + '</div>';
    });
    return html + '</div>';
  }

  function attrIconUrl(attribute) {
    var key = String(attribute || 'ALL').toLowerCase();
    return '/assets/attr/' + key + '.png';
  }

  function formationBuffListHtml(deck) {
    var skills = deck.formationSkills || {};
    var keys = Object.keys(skills);
    if (!keys.length) {
      return '<p class="small">这个阵型没有额外的强化格。</p>';
    }
    var html = '<div class="kvList">';
    keys.sort(function (a, b) { return Number(a) - Number(b); }).forEach(function (slot) {
      var info = skills[slot] || {};
      html += '<div class="kv"><span>强化格 ' + esc(slot) + '</span><strong>'
        + esc(info.description || info.name || '') + '</strong></div>';
    });
    return html + '</div>';
  }

  function deckSummaryHtml(deck) {
    return '<div class="kvList">'
      + '<div class="kv"><span>编成类型</span><strong>' + esc(DECK_KIND_LABEL[deck.kind] || deck.kind)
      + ' · deckType ' + esc(deck.deckType) + '</strong></div>'
      + '<div class="kv"><span>阵型</span><strong>' + esc(deck.formationName || ('#' + deck.formationSheetId))
      + '（格子 ' + esc((deck.formationSlots || []).join(' / ')) + '）</strong></div>'
      + '<div class="kv"><span>成员</span><strong>' + esc(deck.memberCount) + ' / 4</strong></div>'
      + '<div class="kv"><span>结晶</span><strong>' + esc(deck.pieceCount) + '</strong></div>'
      + '</div>';
  }

  function renderDecks() {
    if (S.deckSel) { return renderDeckDetail(); }
    return paintCurrent(api('/api/view/decks'), function (data) {
      var questDecks = (data.decks || []).filter(function (deck) { return deck.kind === 'quest'; });
      var others = (data.decks || []).filter(function (deck) { return deck.kind !== 'quest'; });
      var html = '<div class="toolbar"><span class="small">任务编队 ' + questDecks.length + ' 个</span>'
        + '<button data-formation-manage>阵型管理…</button>'
        + '<span class="spacer"></span><span class="small">点编队进入详情；九宫格是该格在阵型里的站位</span></div>';
      questDecks.forEach(function (deck) {
        html += '<section class="panel deckCard" data-deck="' + esc(deck.deckType) + '" data-deck-open="' + esc(deck.deckType) + '">'
          + '<h2>' + esc(deck.name) + ' <span class="tag">' + esc(DECK_KIND_LABEL[deck.kind]) + '</span>'
          + '<span class="spacer"></span><span class="small">成员 ' + esc(deck.memberCount)
          + ' / 4 · 结晶 ' + esc(deck.pieceCount) + '</span></h2>';
        html += '<p class="small">阵型：' + esc(deck.formationName || ('#' + deck.formationSheetId))
          + '（格子 ' + esc((deck.formationSlots || []).join(' / ')) + '）'
          + (deck.editable ? ' <button class="mini" data-deck-formation>更换阵型</button>' : '')
          + '</p>';
        html += '<div class="deckGrid">';
        (deck.layout || deck.members).forEach(function (item) {
          html += deckSlotHtml(deck, item, false);
        });
        html += '</div></section>';
      });
      if (!questDecks.length) {
        html += '<div class="empty">存档里没有任务编队。</div>';
      }
      if (others.length) {
        html += '<p class="small">存档里另有 ' + others.length + ' 个非任务编成：'
          + others.map(function (deck) {
              return '<button class="mini" data-deck-other="' + esc(deck.deckType) + '">'
                + esc(DECK_KIND_LABEL[deck.kind] || deck.kind) + ' · ' + esc(deck.name) + '</button>';
            }).join(' ')
          + '（只读，支援编成按属性出位，镜界/舞台对战另有规则）</p>';
      }
      $('main').innerHTML = html;

      var manage = $('main').querySelector('[data-formation-manage]');
      if (manage) {
        manage.addEventListener('click', openFormationManager);
      }
      $('main').querySelectorAll('[data-deck-other]').forEach(function (button) {
        button.addEventListener('click', function () {
          S.deckSel = String(button.getAttribute('data-deck-other'));
          render();
        });
      });
      bindDeckSection(questDecks);
    });
  }

  var SUPPORT_LABEL = {
    ALL: 'ALL（不限属性）', FIRE: '火', WATER: '水', TIMBER: '木', LIGHT: '光', DARK: '暗',
  };

  function renderDeckDetail() {
    return paintCurrent(api('/api/view/decks'), function (data) {
      var deck = (data.decks || []).filter(function (item) {
        return String(item.deckType) === String(S.deckSel);
      })[0];
      if (!deck) {
        S.deckSel = null;
        fail(new Error('找不到这个编成'));
        return renderDecks();
      }
      var html = '<div class="crumbs"><button class="mini" data-deck-back>队伍一览</button>'
        + '<span>›</span><span class="small">' + esc(deck.name) + '</span></div>';
      html += '<div data-deck="' + esc(deck.deckType) + '">';
      html += '<section class="panel"><h2>' + esc(deck.name) + ' <span class="tag">'
        + esc(DECK_KIND_LABEL[deck.kind] || deck.kind) + '</span>'
        + '<span class="spacer"></span><span class="small">成员 ' + esc(deck.memberCount)
        + '</span></h2>' + deckSummaryHtml(deck);

      if (deck.kind === 'support') {
        html += '<p class="small">支援编成有 6 个按属性的槽位（ALL / 火 / 水 / 木 / 光 / 暗），没有阵型与九宫格，'
          + '这一页只做查看。</p></section>';
        html += '<section class="panel"><h2>属性槽位</h2><div class="grid wide">';
        (deck.layout || []).forEach(function (item) {
          html += '<div class="tile"><div class="name">'
            + esc(SUPPORT_LABEL[item.attribute] || ('槽 ' + esc(item.slot))) + '</div>';
          if (item.userCardId) {
            html += '<div class="row">' + thumb(item.face || item.icon, item.name, 'faceIcon')
              + '<div><div class="name">' + esc(item.name) + '</div>'
              + '<div class="id">Lv ' + esc(item.level) + ' · 突破 ' + esc(item.revision) + '</div></div></div>';
          } else {
            html += '<div class="id">空位</div>';
          }
          html += '</div>';
        });
        html += '</div></section></div>';
        $('main').innerHTML = html;
        var backBtn = $('main').querySelector('[data-deck-back]');
        if (backBtn) {
          backBtn.addEventListener('click', function () {
            S.deckSel = null;
            render();
          });
        }
        return;
      }

      html += '<div class="row">'
        + (deck.editable ? '<button class="mini" data-deck-formation>更换阵型</button>' : '')
        + '<button class="mini" data-formation-manage>阵型管理…</button>'
        + '</div></section>';
      html += '<section class="panel"><h2>站位总览</h2>' + deckBoardHtml(deck)
        + formationBuffListHtml(deck)
        + '<p class="small">格子编号按游戏规则（3 6 9 / 2 5 8 / 1 4 7），角标「强」就是阵型的强化格。</p></section>';
      html += '<section class="panel"><h2>成员与结晶</h2><div class="deckGrid detail">';
      (deck.layout || []).forEach(function (item) {
        html += deckSlotHtml(deck, item, true);
      });
      html += '</div>';
      if (!deck.editable) {
        html += '<p class="small">这个编成不是任务编队，页面只做查看。</p>';
      }
      html += '</section></div>';
      $('main').innerHTML = html;

      var back = $('main').querySelector('[data-deck-back]');
      if (back) {
        back.addEventListener('click', function () {
          S.deckSel = null;
          render();
        });
      }
      var manage = $('main').querySelector('[data-formation-manage]');
      if (manage) {
        manage.addEventListener('click', openFormationManager);
      }
      bindDeckSection([deck]);
    });
  }

  function bindDeckSection(decks) {
    decks.forEach(function (deck) {
      var section = $('main').querySelector('[data-deck="' + deck.deckType + '"]');
      if (!section) { return; }
      var deckType = deck.deckType;
      if (section.getAttribute('data-deck-open')) {
        section.addEventListener('click', function (event) {
          if (event.target.closest('button, input, [data-goto-chara], [data-goto-piece]')) { return; }
          S.deckSel = String(deckType);
          render();
        });
      }
      var formationButton = section.querySelector('[data-deck-formation]');
      if (formationButton) {
        formationButton.addEventListener('click', function () {
          openFormationPicker(deckType);
        });
      }
      var helperButton = section.querySelector('[data-deck-helper]');
      if (helperButton) {
        helperButton.addEventListener('click', function () {
          openHelperPicker(deckType);
        });
      }
      section.querySelectorAll('[data-goto-chara]').forEach(function (tile) {
        tile.addEventListener('click', function () {
          S.tab = 'charas';
          S.charaSel = tile.getAttribute('data-goto-chara');
          S.charaPanel = null;
          S.spiritSel = null;
          renderTabs();
          render();
        });
      });
      section.querySelectorAll('[data-goto-piece]').forEach(function (tile) {
        tile.addEventListener('click', function (event) {
          if (event.target.closest('button')) { return; }
          S.tab = 'pieces';
          S.pieceSel = tile.getAttribute('data-goto-piece');
          renderTabs();
          render();
        });
      });
      section.querySelectorAll('[data-empty-position]').forEach(function (button) {
        button.addEventListener('click', function () {
          var free = (deck.members || []).filter(function (member) { return !member.userCardId; })[0];
          if (!free) { toast('4 个成员位都满了，先移除一个', 'err'); return; }
          openDeckMemberPicker(deckType, free.slot, Number(button.getAttribute('data-empty-position')));
        });
      });
      section.querySelectorAll('[data-deck-member]').forEach(function (button) {
        button.addEventListener('click', function () {
          openDeckMemberPicker(deckType, Number(button.getAttribute('data-deck-member')));
        });
      });
      section.querySelectorAll('[data-deck-member-remove]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'deck.removeMember', deckType: deckType,
            slot: Number(button.getAttribute('data-deck-member-remove')) }]);
        });
      });
      section.querySelectorAll('[data-deck-swap]').forEach(function (button) {
        button.addEventListener('click', function () {
          var from = button.getAttribute('data-deck-swap');
          openSwapPicker(deckType, from === 'helper' ? 'helper' : Number(from));
        });
      });
      section.querySelectorAll('[data-deck-piece-add]').forEach(function (button) {
        button.addEventListener('click', function () {
          var slotEl = button.closest('[data-member]');
          var member = Number(slotEl.getAttribute('data-member'));
          var used = {};
          slotEl.querySelectorAll('[data-slot]').forEach(function (item) {
            used[Number(item.getAttribute('data-slot'))] = true;
          });
          var free = 0;
          for (var index = 1; index <= 5; index += 1) {
            if (!used[index]) { free = index; break; }
          }
          if (!free) { toast('这个角色 5 个结晶槽都满了', 'err'); return; }
          openDeckPiecePicker(deckType, member, free);
        });
      });
      section.querySelectorAll('[data-deck-piece-remove]').forEach(function (button) {
        button.addEventListener('click', function (event) {
          event.stopPropagation();
          var slotEl = button.closest('[data-member]');
          edit([{ op: 'deck.removePiece', deckType: deckType,
            member: Number(slotEl.getAttribute('data-member')),
            slot: Number(button.getAttribute('data-slot')) }]);
        });
      });
    });
  }

  function openSwapPicker(deckType, from) {
    api('/api/view/decks').then(function (data) {
      var deck = (data.decks || []).filter(function (item) {
        return String(item.deckType) === String(deckType);
      })[0];
      if (!deck) { fail(new Error('找不到该编成')); return; }
      openPicker({
        title: '换位（点九宫格）',
        placeholder: '阵型格子',
        search: false,
        load: function () {
          return Promise.resolve({ rows: [{ html: slotGridHtml(deck, from, false) }], total: 1 });
        },
        meta: function () {
          return '阵型：' + (deck.formationName || deck.formationSheetId);
        },
        render: function (rows) { return (rows[0] && rows[0].html) || ''; },
        bind: function (host) { bindSlotGrid(host, deckType, from); }
      });
    }).catch(fail);
  }

  function openFormationPicker(deckType) {
    var rowsAll = [];
    api('/api/view/formations').then(function (data) {
      rowsAll = (data.rows || []).filter(function (row) { return row.owned; });
      openPicker({
        title: '更换阵型（只列已获取的）',
        placeholder: '搜索阵型名',
        load: function (query) {
          var needle = String(query || '').toLowerCase();
          var rows = rowsAll.filter(function (row) {
            return !needle || String(row.name || '').toLowerCase().indexOf(needle) >= 0
              || String(row.formationSheetId).indexOf(needle) >= 0;
          });
          return Promise.resolve({ rows: rows, total: rows.length });
        },
        meta: function (data2) { return '已获取 ' + data2.total + ' 个阵型'; },
        render: function (rows) {
          var html = '<div class="grid wide">';
          rows.forEach(function (row) {
            html += '<div class="tile"><div class="name">' + esc(row.name) + '</div>'
              + '<div class="id">#' + esc(row.formationSheetId) + ' · 格子 ' + esc((row.slots || []).join('/')) + '</div>'
              + '<div class="small">' + esc(row.description || '') + '</div>'
              + '<div class="row"><button class="mini" data-formation-pick="' + esc(row.formationSheetId) + '">用这个</button></div></div>';
          });
          return html + '</div>';
        },
        bind: function (host) {
          host.querySelectorAll('[data-formation-pick]').forEach(function (button) {
            button.addEventListener('click', function () {
              edit([{ op: 'deck.formation', deckType: deckType,
                formationSheetId: Number(button.getAttribute('data-formation-pick')) }]);
            });
          });
        }
      });
    }).catch(fail);
  }

  function slotGridHtml(deck, currentKey, allowEmpty) {
    var slotSet = {};
    (deck.formationSlots || []).forEach(function (slot) { slotSet[slot] = true; });
    var taken = {};
    (deck.members || []).forEach(function (member) {
      if (member.userCardId && member.position) { taken[member.position] = member; }
    });
    var html = '<div class="slotPicker">';
    POSITION_VISUAL_ORDER.forEach(function (slot) {
      var inPool = slotSet[slot];
      var isHelper = slot === deck.helperPosition;
      var member = taken[slot];
      var key = isHelper ? 'helper' : (member ? String(member.slot) : '');
      var isCurrent = key && String(key) === String(currentKey);
      var buff = (deck.formationSkills || {})[String(slot)];
      var cls = 'slotCell';
      if (buff) { cls += ' effective'; }
      if (!inPool) { cls += ' disabled'; }
      else if (isHelper) { cls += ' helper'; }
      else if (member) { cls += ' occupied'; }
      else { cls += ' free'; }
      if (isCurrent) { cls += ' current'; }
      html += '<div class="' + cls + '"><span class="pos">' + slot + '</span>';
      if (buff) {
        html += '<img class="buffMark" src="' + esc(attrIconUrl(buff.attribute))
          + '" title="' + esc(buff.description || buff.name || '') + '" alt="' + esc(buff.attribute || '') + '">';
      }
      if (isHelper) { html += '<span class="who">支援位</span>'; }
      else if (member) { html += '<span class="who">' + esc(member.name || '') + '</span>'; }
      else if (inPool) { html += '<span class="who">空位</span>'; }
      if (isCurrent) {
        html += '<span class="small">当前位置</span>';
      } else if (inPool && isHelper) {
        html += '<button class="mini" data-slot-swap="helper">换到这里</button>';
      } else if (member) {
        html += '<button class="mini" data-slot-swap="' + esc(member.slot) + '">换到这里</button>';
      } else if (inPool && allowEmpty) {
        html += '<button class="mini" data-slot-set="' + slot + '">放这里</button>';
      }
      html += '</div>';
    });
    return html + '</div>';
  }

  function bindSlotGrid(host, deckType, fromKey) {
    host.querySelectorAll('[data-slot-set]').forEach(function (button) {
      button.addEventListener('click', function () {
        edit([{ op: 'deck.helper', deckType: deckType,
          position: Number(button.getAttribute('data-slot-set')) }]);
      });
    });
    host.querySelectorAll('[data-slot-swap]').forEach(function (button) {
      button.addEventListener('click', function () {
        var target = button.getAttribute('data-slot-swap');
        edit([{ op: 'deck.swap', deckType: deckType, a: fromKey,
          b: target === 'helper' ? 'helper' : Number(target) }]);
      });
    });
  }

  function openHelperPicker(deckType) {
    api('/api/view/decks').then(function (data) {
      var deck = (data.decks || []).filter(function (item) {
        return String(item.deckType) === String(deckType);
      })[0];
      if (!deck) { fail(new Error('找不到该编成')); return; }
      openPicker({
        title: '调整支援位（点九宫格）',
        placeholder: '阵型格子',
        search: false,
        load: function () {
          return Promise.resolve({ rows: [{ html: slotGridHtml(deck, 'helper', true) }], total: 1 });
        },
        meta: function () {
          return '阵型：' + (deck.formationName || deck.formationSheetId)
            + ' · 当前支援位：' + (deck.helperPosition || '—');
        },
        render: function (rows) { return (rows[0] && rows[0].html) || ''; },
        bind: function (host) { bindSlotGrid(host, deckType, 'helper'); }
      });
    }).catch(fail);
  }

  function openFormationManager() {
    api('/api/view/formations').then(function (data) {
      var rowsAll = data.rows || [];
      openPicker({
        title: '阵型获取状态',
        placeholder: '搜索阵型名',
        load: function (query) {
          var needle = String(query || '').toLowerCase();
          var rows = rowsAll.filter(function (row) {
            return !needle || String(row.name || '').toLowerCase().indexOf(needle) >= 0;
          });
          return Promise.resolve({ rows: rows, total: rows.length });
        },
        meta: function () { return '已获取 ' + data.ownedCount + ' / ' + data.total; },
        render: function (rows) {
          var html = '<div class="grid wide">';
          rows.forEach(function (row) {
            html += '<div class="tile"><div class="name">' + esc(row.name) + '</div>'
              + '<div class="id">#' + esc(row.formationSheetId) + ' · 格子 ' + esc((row.slots || []).join('/')) + '</div>'
              + '<div class="small">' + esc(row.description || '') + '</div>'
              + '<div class="tags">' + (row.owned ? '<span class="tag ok">已获取</span>'
                : '<span class="tag">未获取</span>')
              + (row.usedBy && row.usedBy.length ? '<span class="tag">' + esc(row.usedBy.join('、')) + '</span>' : '')
              + '</div><div class="row">'
              + (row.owned
                ? '<button class="mini" data-formation-remove="' + esc(row.formationSheetId) + '">取消获取</button>'
                : '<button class="mini" data-formation-add="' + esc(row.formationSheetId) + '">获取</button>')
              + '</div></div>';
          });
          return html + '</div>';
        },
        bind: function (host) {
          host.querySelectorAll('[data-formation-add]').forEach(function (button) {
            button.addEventListener('click', function () {
              edit([{ op: 'formation.add',
                formationSheetId: Number(button.getAttribute('data-formation-add')) }]);
            });
          });
          host.querySelectorAll('[data-formation-remove]').forEach(function (button) {
            button.addEventListener('click', function () {
              edit([{ op: 'formation.remove',
                formationSheetId: Number(button.getAttribute('data-formation-remove')) }]);
            });
          });
        }
      });
    }).catch(fail);
  }

  function openDeckMemberPicker(deckType, slot, position) {
    openPicker({
      title: '选择上阵角色（第 ' + slot + ' 位）',
      placeholder: '搜索角色或卡名',
      load: function (q) { return api('/api/view/charas?limit=200&q=' + encodeURIComponent(q)); },
      meta: function (data) { return '已拥有 ' + data.total + ' 名'; },
      render: function (rows) {
        var html = '<div class="grid wide">';
        rows.forEach(function (row) {
          html += '<div class="tile">' + thumb(row.icon, row.name, 'icon')
            + '<div class="name">' + esc(row.name) + '</div>'
            + '<div class="id">' + esc(row.cardName) + ' · Lv ' + esc(row.level) + '</div>'
            + '<div class="row"><button class="mini" data-deck-pick="' + esc(row.userCardId) + '">放入</button></div></div>';
        });
        return html + '</div>';
      },
      bind: function (host, rows) {
        host.querySelectorAll('[data-deck-pick]').forEach(function (button) {
          button.addEventListener('click', function () {
            var op = { op: 'deck.member', deckType: deckType, slot: slot,
              userCardId: button.getAttribute('data-deck-pick') };
            if (position) { op.position = Number(position); }
            edit([op]);
          });
        });
      }
    });
  }

  function openDeckPiecePicker(deckType, member, slot) {
    var filter = { rank: '', type: '' };
    openPicker({
      title: '给第 ' + member + ' 位（槽 ' + slot + '）选择记忆结晶',
      placeholder: '搜索结晶名称',
      load: function (q) {
        return api('/api/view/pieces?limit=200&q=' + encodeURIComponent(q)
          + '&rank=' + encodeURIComponent(filter.rank)
          + '&type=' + encodeURIComponent(filter.type));
      },
      meta: function (data) { return '已拥有 ' + data.total + ' 枚'; },
      render: function (rows, data) {
        var html = '<div class="toolbar">'
          + '<select data-dp-rank>' + selectOptions(data.ranks || [], filter.rank) + '</select>'
          + '<select data-dp-type>' + selectOptions(data.types || [], filter.type, pieceTypeLabel) + '</select>'
          + '<span class="small">' + rows.length + ' 枚</span></div>'
          + '<div class="grid wide">';
        rows.forEach(function (row) {
          html += '<div class="tile">' + thumb(row.icon, row.name, 'icon')
            + '<div class="name">' + esc(row.name) + '</div>'
            + '<div class="id">#' + esc(row.pieceId) + ' · ' + esc(pieceRankLabel(row.rank)) + ' · '
            + esc(pieceTypeLabel(row.type)) + ' · Lv ' + esc(row.level) + '</div>'
            + '<div class="row"><button class="mini" data-deck-piece="' + esc(row.id) + '">装上</button></div></div>';
        });
        return html + '</div>';
      },
      bind: function (host, rows) {
        var rankSelect = host.querySelector('[data-dp-rank]');
        if (rankSelect) {
          rankSelect.addEventListener('change', function () {
            filter.rank = rankSelect.value;
            if (host._reload) { host._reload(); }
          });
        }
        var typeSelect = host.querySelector('[data-dp-type]');
        if (typeSelect) {
          typeSelect.addEventListener('change', function () {
            filter.type = typeSelect.value;
            if (host._reload) { host._reload(); }
          });
        }
        host.querySelectorAll('[data-deck-piece]').forEach(function (button) {
          button.addEventListener('click', function () {
            edit([{ op: 'deck.piece', deckType: deckType, member: member, slot: slot,
              pieceInstanceId: button.getAttribute('data-deck-piece') }]);
          });
        });
      }
    });
  }

  function closePreview() {
    var node = document.getElementById('previewModal');
    if (node) {
      node.parentNode.removeChild(node); syncDialogLayer();
      if (!focusInView(previewOpener)) { focusInView($('modalClose')); }
      previewOpener = null;
    }
  }

  function openPreview(title, html, trigger) {
    closePreview();
    previewOpener = trigger || document.activeElement;
    var host = document.createElement('div');
    host.id = 'previewModal';
    host.className = 'modal show';
    host.innerHTML = '<div class="panel" role="dialog" aria-modal="true" aria-labelledby="piecePreviewTitle">'
      + '<div class="head"><strong id="piecePreviewTitle">' + esc(title) + '</strong><span class="spacer"></span>'
      + '<button class="ghost mini" data-preview-close>关闭</button></div>'
      + '<div class="body">' + html + '</div></div>';
    document.body.appendChild(host); syncDialogLayer();
    focusInView(host.querySelector('[data-preview-close]'));
    host.addEventListener('click', function (event) {
      if (event.target === host) { closePreview(); }
      else if (event.target.closest('[data-preview-close]')) { closePreview(); }
    });
  }

  function piecePreviewHtml(d) {
    var html = '<div class="previewHead">'
      + '<img class="icon" src="/assets/pieces/' + esc(d.pieceId) + '.png" alt="">'
      + '<div><div class="name">' + esc(d.name || '') + '</div>'
      + '<div class="small">' + esc(pieceTypeLabel(d.type)) + ' · ' + esc(d.rank || '')
      + (d.attribute ? ' · ' + esc(d.attribute) : '') + '</div></div></div>';
    if (d.base) {
      html += '<div class="small">基础：HP ' + fmt(d.base.hp) + ' / 攻 ' + fmt(d.base.attack)
        + ' / 防 ' + fmt(d.base.defense) + '</div>';
    }
    if (d.statsAtMax) {
      html += '<div class="small">满级 Lv' + esc(d.maxLevel) + '：HP ' + fmt(d.statsAtMax.hp)
        + ' / 攻 ' + fmt(d.statsAtMax.attack) + ' / 防 ' + fmt(d.statsAtMax.defense) + '</div>';
    }
    if (d.description) {
      html += '<p class="pieceDesc small">' + esc(nl(d.description)) + '</p>';
    }
    if ((d.limitCharaNames || []).length) {
      html += '<div class="small warn">仅限：' + esc(d.limitCharaNames.join('、')) + '</div>';
    }
    (d.skills || []).forEach(function (skill) {
      html += '<div class="small"><b>' + esc(skill.name || ('技能 ' + skill.skillId)) + '</b> '
        + (skill.intervalTurn ? '<span class="tag">冷却 ' + esc(skill.intervalTurn) + ' 回合</span> ' : '')
        + esc(nl(skill.description)) + '</div>';
    });
    return html;
  }

  function openPiecePicker() {
    var filter = { rank: '', type: '', limit: '', charaId: '' };
    var charaOptions = S.charaOptions || [];
    function fetch(query) {
      return api('/api/view/pieces-catalog?limit=200&q=' + encodeURIComponent(query)
        + '&rank=' + encodeURIComponent(filter.rank)
        + '&type=' + encodeURIComponent(filter.type)
        + '&limitMode=' + encodeURIComponent(filter.limit)
        + '&charaId=' + encodeURIComponent(filter.charaId));
    }
    var ready = charaOptions.length
      ? Promise.resolve()
      : api('/api/view/charas?limit=200').then(function (data) {
          S.charaOptions = (data.rows || []).map(function (row) {
            return { charaId: row.charaId, name: row.name };
          });
          charaOptions = S.charaOptions;
        }).catch(function () {});
    ready.then(function () {
      openPicker({
        title: '添加记忆结晶',
        placeholder: '搜索结晶名称或 pieceId（留空列出全部已实装）',
        load: fetch,
        meta: function (data) { return '匹配 ' + data.total + ' 条，可选等级与突破'; },
        render: function (rows, data) {
          var html = '<div class="toolbar"><select data-pick-rank>'
            + selectOptions(data && data.ranks, filter.rank) + '</select>'
            + '<select data-pick-type>' + selectOptions(data && data.types, filter.type, pieceTypeLabel)
            + '</select>'
            + '<select data-pick-limit>'
            + '<option value="">限制：全部</option>'
            + '<option value="limited"' + (filter.limit === 'limited' ? ' selected' : '') + '>限制：仅限指定角色</option>'
            + '<option value="free"' + (filter.limit === 'free' ? ' selected' : '') + '>限制：通用</option>'
            + '</select>'
            + '<select data-pick-chara><option value="">角色：全部</option>'
            + charaOptions.map(function (row) {
                return '<option value="' + esc(row.charaId) + '"'
                  + (String(row.charaId) === String(filter.charaId) ? ' selected' : '') + '>'
                  + esc(row.name) + '</option>';
              }).join('')
            + '</select><span class="small">筛选立即生效</span></div><div class="grid wide">';
          rows.forEach(function (row) {
            html += tileHtml(
              thumb(row.icon, row.name, 'icon'),
              row.name,
              '#' + row.pieceId + ' · ' + pieceTypeLabel(row.type),
              row.addable
                ? '<div class="row"><input data-lv type="number" min="1" value="1"><span class="small">lv</span>'
                  + '<input data-lb type="number" min="0" max="4" value="0"><span class="small">突</span></div>'
                  + '<div class="row"><button class="mini" data-add>添加</button>'
                  + '<button class="mini" data-preview="' + esc(row.pieceId) + '">预览</button></div>'
                : '<div class="row"><span class="tag warn">缺成长数据</span></div>',
              '<span class="tag">' + esc(row.rank || '') + '</span>'
                + (row.ownedCount ? '<span class="tag ok">已有 ' + row.ownedCount + '</span>' : '')
            );
          });
          return html + '</div>';
        },
        bind: function (host, rows) {
          var limitSelect = host.querySelector('[data-pick-limit]');
          if (limitSelect) {
            limitSelect.addEventListener('change', function () {
              filter.limit = limitSelect.value;
              if (host._reload) { host._reload(); }
            });
          }
          var charaSelect = host.querySelector('[data-pick-chara]');
          if (charaSelect) {
            charaSelect.addEventListener('change', function () {
              filter.charaId = charaSelect.value;
              if (host._reload) { host._reload(); }
            });
          }
          var rankSelect = host.querySelector('[data-pick-rank]');
          if (rankSelect) {
            rankSelect.addEventListener('change', function () {
              filter.rank = rankSelect.value;
              if (host._reload) { host._reload(); }
            });
          }
          var typeSelect = host.querySelector('[data-pick-type]');
          if (typeSelect) {
            typeSelect.addEventListener('change', function () {
              filter.type = typeSelect.value;
              if (host._reload) { host._reload(); }
            });
          }
          host.querySelectorAll('[data-add]').forEach(function (button, index) {
            button.addEventListener('click', function (event) {
              var tileEl = button.closest('.tile');
              var level = readInteger(tileEl.querySelector('[data-lv]'));
              if (level === null) { event.preventDefault(); return; }
              var lb = readInteger(tileEl.querySelector('[data-lb]'));
              if (lb === null) { event.preventDefault(); return; }
              edit([{ op: 'piece.add', pieceId: rows[index].pieceId, level: level, lbCount: lb }]);
            });
          });
          host.querySelectorAll('[data-preview]').forEach(function (button) {
            button.addEventListener('click', function () {
              api('/api/view/piece-master?pieceId=' + encodeURIComponent(button.getAttribute('data-preview')))
                .then(function (detail) {
                  if (button.isConnected && $('modal').classList.contains('show')) {
                    openPreview(detail.name || ('结晶 ' + detail.pieceId), piecePreviewHtml(detail), button);
                  }
                }).catch(fail);
            });
          });
        }
      });
    });
  }

  function openState(tabId, key, fallback) {
    S.open = S.open || {};
    S.open[tabId] = S.open[tabId] || {};
    if (S.open[tabId][key] === undefined) { return fallback ? ' open' : ''; }
    return S.open[tabId][key] ? ' open' : '';
  }

  function bindTree(tabId) {
    $('main').querySelectorAll('details.tree').forEach(function (node) {
      node.addEventListener('toggle', function () {
        S.open = S.open || {};
        S.open[tabId] = S.open[tabId] || {};
        S.open[tabId][node.getAttribute('data-key')] = node.open;
      });
    });
    $('main').querySelectorAll('summary button').forEach(function (button) {
      button.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
      });
    });
  }

  function sectionCountOf(part) {
    var total = 0;
    (part.chapters || []).forEach(function (chapter) { total += (chapter.sections || []).length; });
    return total;
  }

  function renderQuests() {
    var tabId = 'quests';
    var page = pageOf(tabId);
    var url = '/api/view/quests-tree?q=' + encodeURIComponent(page.q || '')
      + (page.only ? '&only=' + page.only : '')
      + (page.catalog ? '&catalog=1' : '');
    return paintCurrent(api(url), function (data) {
      var parts = data.parts || [];
      var html = '<div class="toolbar"><input type="text" data-search placeholder="搜索部、章、话或任务 ID">'
        + '<select data-only><option value="">全部</option><option value="uncleared">未通关</option>'
        + '<option value="cleared">已通关</option></select>'
        + '<button data-clear-all>全部标记通关</button><button data-reset-all>全部标记未通关</button>'
        + '<select data-catalog><option value="0">仅存档收录</option>'
        + '<option value="1">含数据包全量</option></select>'
        + '<span class="spacer"></span><span class="small">' + parts.length + ' 部 / 分类，共 '
        + data.total + ' 个任务（存档 ' + data.storedTotal + ' / 数据包 ' + data.catalogTotal
        + '）</span></div>';
      if (!parts.length) {
        html += '<div class="empty">没有匹配的任务</div>';
      }
      parts.forEach(function (part, partIndex) {
        html += '<details class="tree" data-key="' + esc(part.key) + '"'
          + openState(tabId, part.key, partIndex === 0) + '>'
          + '<summary><b>' + esc(part.label) + '</b> <span class="small">' + part.chapterCount + ' 章 · '
          + sectionCountOf(part) + ' 话 · 已通 ' + part.cleared + ' / ' + part.total + '</span></summary>'
          + '<div class="treeBody">';
        (part.chapters || []).forEach(function (chapter, chapterIndex) {
          html += '<details class="tree level2" data-key="' + esc(chapter.key) + '"'
            + openState(tabId, chapter.key, partIndex === 0 && chapterIndex === 0) + '>'
            + '<summary>' + esc(chapter.label) + ' <span class="small">' + chapter.sectionCount
            + ' 话 · 已通 ' + chapter.cleared + ' / ' + chapter.total + '</span></summary>'
            + '<div class="treeBody">';
          (chapter.sections || []).forEach(function (section) {
            var secKey = chapter.key + '|' + section.sectionId;
            html += '<details class="tree level3" data-key="' + esc(secKey) + '"'
              + openState(tabId, secKey, false) + '>'
              + '<summary>' + esc(section.title || section.sectionId)
              + ' <span class="small">#' + esc(section.sectionId)
              + (section.area ? ' · ' + esc(section.area) : '')
              + ' · 已通 ' + section.cleared + ' / ' + section.total + '</span>'
              + ' <button class="mini" data-sec="' + esc(section.sectionId) + '" data-sec-cleared="1">本话全清</button>'
              + ' <button class="mini" data-sec="' + esc(section.sectionId) + '" data-sec-cleared="0">本话清空</button>'
              + '</summary><div class="treeBody">'
              + '<table><thead><tr><th>任务 ID</th><th>状态</th><th>次数</th><th>任务 1/2/3</th><th></th></tr></thead><tbody>';
            (section.quests || []).forEach(function (quest) {
              html += '<tr><td><b>' + esc(quest.label || ('#' + quest.questBattleId)) + '</b>'
                + ' <span class="small">#' + esc(quest.questBattleId) + '</span></td>'
                + '<td>' + (quest.stored === false
                  ? '<span class="tag">未收录</span>'
                  : quest.cleared ? '<span class="tag ok">已通关</span>' : '<span class="tag">未通关</span>')
                + '</td><td class="num">' + (quest.stored === false ? '—' : fmt(quest.clearCount)) + '</td>'
                + '<td class="small missions">' + (quest.missions || []).map(function (m, mi) {
                  return '<button class="mini" data-quest-mission="' + esc(quest.questBattleId)
                    + '" data-mission-index="' + (mi + 1) + '" data-mission-on="'
                    + (m === 'CLEAR' ? '1' : '0') + '">' + (m === 'CLEAR' ? '✔' : '·') + '</button>';
                }).join('') + '</td>'
                + '<td>' + (quest.stored === false
                  ? '<button class="mini" data-ensure-quest="' + esc(quest.questBattleId) + '">补行</button>'
                  : '<button class="mini" data-quest="' + esc(quest.questBattleId) + '" data-quest-cleared="'
                    + (quest.cleared ? '1' : '0') + '">切换</button>')
                + '</td></tr>';
            });
            html += '</tbody></table></div></details>';
          });
          html += '</div></details>';
        });
        html += '</div></details>';
      });
      $('main').innerHTML = html;
      searchBox(tabId, '搜索任务', render);
      bindTree(tabId);
      var catalogSelect = $('main').querySelector('[data-catalog]');
      catalogSelect.value = page.catalog ? '1' : '0';
      catalogSelect.addEventListener('change', function () {
        page.catalog = catalogSelect.value === '1';
        render();
      });
      $('main').querySelectorAll('[data-ensure-quest]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'quest.ensure', questBattleId: Number(button.getAttribute('data-ensure-quest')) }]);
        });
      });
      var onlySelect = $('main').querySelector('[data-only]');
      onlySelect.value = page.only || '';
      onlySelect.addEventListener('change', function () {
        page.only = onlySelect.value;
        page.offset = 0;
        render();
      });
      $('main').querySelectorAll('[data-quest]').forEach(function (button) {
        button.addEventListener('click', function () {
          var questId = Number(button.getAttribute('data-quest'));
          var cleared = button.getAttribute('data-quest-cleared') === '1';
          edit([{ op: 'quest.set', questBattleId: questId, cleared: !cleared,
            clearCount: cleared ? 0 : 1 }]);
        });
      });
      $('main').querySelectorAll('[data-quest-mission]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'quest.mission',
            questBattleId: Number(button.getAttribute('data-quest-mission')),
            index: Number(button.getAttribute('data-mission-index')),
            status: button.getAttribute('data-mission-on') === '1' ? 'NON_CLEAR' : 'CLEAR' }]);
        });
      });
      $('main').querySelectorAll('[data-sec]').forEach(function (button) {
        button.addEventListener('click', function () {
          var sectionId = Number(button.getAttribute('data-sec'));
          var cleared = button.getAttribute('data-sec-cleared') === '1';
          edit([{ op: 'quest.bulk', cleared: cleared, missions: true,
            onlyUncleared: cleared, sectionIds: [sectionId] }]);
        });
      });
      $('main').querySelector('[data-clear-all]').addEventListener('click', function () {
        if (!window.confirm('把全部未通关任务标记为已通关（含任务 1/2/3）？')) { return; }
        edit([{ op: 'quest.bulk', cleared: true, missions: true, onlyUncleared: true }]);
      });
      $('main').querySelector('[data-reset-all]').addEventListener('click', function () {
        if (!window.confirm('把全部任务重置为未通关？')) { return; }
        edit([{ op: 'quest.bulk', cleared: false, missions: true, onlyUncleared: false }]);
      });
    });
  }

  // ------------------------------------------------------------ 小节 / 章节

  function renderSections() {
    var tabId = 'sections';
    var page = pageOf(tabId);
    var url = '/api/view/sections-tree?q=' + encodeURIComponent(page.q || '')
      + (page.catalog ? '&catalog=1' : '');
    return paintCurrent(api(url), function (data) {
      var parts = data.parts || [];
      var html = '<div class="toolbar"><input type="text" data-search placeholder="搜索部、章、区域或小节 ID">'
        + '<button data-unlock>全部设为可玩</button><button data-clear>全部标记已通</button>'
        + '<select data-catalog><option value="0">仅存档收录</option>'
        + '<option value="1">含数据包全量</option></select>'
        + '<span class="spacer"></span><span class="small">' + parts.length + ' 部 / 分类，共 '
        + data.total + ' 个小节（存档 ' + data.storedTotal + ' / 数据包 ' + data.catalogTotal
        + '）</span></div>';
      if (!parts.length) {
        html += '<div class="empty">没有匹配的小节</div>';
      }
      parts.forEach(function (part, partIndex) {
        html += '<details class="tree" data-key="' + esc(part.key) + '"'
          + openState(tabId, part.key, partIndex === 0) + '>'
          + '<summary><b>' + esc(part.label) + '</b> <span class="small">' + part.chapterCount + ' 章 · '
          + sectionCountOf(part) + ' 话 · 已通 ' + part.cleared + ' / ' + part.total + '</span></summary>'
          + '<div class="treeBody">';
        (part.chapters || []).forEach(function (chapter, chapterIndex) {
          html += '<details class="tree level2" data-key="' + esc(chapter.key) + '"'
            + openState(tabId, chapter.key, partIndex === 0 && chapterIndex === 0) + '>'
            + '<summary>' + esc(chapter.label) + ' <span class="small">' + chapter.sectionCount
            + ' 话 · 已通 ' + chapter.cleared + ' / ' + chapter.total + '</span></summary>'
            + '<div class="treeBody"><table><thead><tr><th>小节 ID</th><th>标题</th><th>区域</th>'
            + '<th>类型</th><th>可玩</th><th>已通</th></tr></thead><tbody>';
          (chapter.sections || []).forEach(function (section) {
            html += '<tr><td><b>' + esc(section.title || ('#' + section.sectionId)) + '</b>'
              + ' <span class="small">#' + esc(section.sectionId) + '</span></td><td class="small">'
              + esc(section.area || '') + '</td><td class="small">' + esc(section.questType || '') + '</td><td>'
              + '<button class="mini toggle" data-sec-play="' + esc(section.sectionId) + '" data-on="'
              + (section.canPlay ? '1' : '0') + '">'
              + (section.stored === false ? '补行'
                : section.canPlay ? '可玩' : '锁定') + '</button></td><td>'
              + (section.stored === false
                ? '<button class="mini" data-ensure-section="' + esc(section.sectionId) + '">补行</button>'
                : '<button class="mini toggle" data-sec-clear="' + esc(section.sectionId) + '" data-on="'
                  + (section.cleared ? '1' : '0') + '">' + (section.cleared ? '已通' : '未通') + '</button>')
              + '</td></tr>';
          });
          html += '</tbody></table></div></details>';
        });
        html += '</div></details>';
      });
      $('main').innerHTML = html;
      searchBox(tabId, '搜索小节', render);
      bindTree(tabId);
      $('main').querySelectorAll('[data-sec-play]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'section.set', sectionId: Number(button.getAttribute('data-sec-play')),
            canPlay: button.getAttribute('data-on') !== '1' }]);
        });
      });
      $('main').querySelectorAll('[data-sec-clear]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'section.set', sectionId: Number(button.getAttribute('data-sec-clear')),
            cleared: button.getAttribute('data-on') !== '1' }]);
        });
      });
      var catalogSelect = $('main').querySelector('[data-catalog]');
      catalogSelect.value = page.catalog ? '1' : '0';
      catalogSelect.addEventListener('change', function () {
        page.catalog = catalogSelect.value === '1';
        render();
      });
      $('main').querySelectorAll('[data-ensure-section]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'section.ensure', sectionId: Number(button.getAttribute('data-ensure-section')) }]);
        });
      });
      $('main').querySelector('[data-unlock]').addEventListener('click', function () {
        if (!window.confirm('把全部小节设为可玩？')) { return; }
        edit([{ op: 'section.bulk', canPlay: true }]);
      });
      $('main').querySelector('[data-clear]').addEventListener('click', function () {
        if (!window.confirm('把全部小节标记为已通关？')) { return; }
        edit([{ op: 'section.bulk', canPlay: true, cleared: true }]);
      });
    });
  }

  // 布局照搬游戏分支地图（LostRecordMap.js）：
  //   x = (pointX-1)*66 + 20, y = (pointY-1)*48 + 20
  // 节点落在 66x48 的格子里（比格小一圈，避免相邻重叠），连线走节点中心
  var MAP_CELL_W = 66, MAP_CELL_H = 48, MAP_PAD = 20, MAP_NODE_W = 60, MAP_NODE_H = 44;
  var MAP_NODE_OFF_X = (MAP_CELL_W - MAP_NODE_W) / 2;
  var MAP_NODE_OFF_Y = (MAP_CELL_H - MAP_NODE_H) / 2;

  function chapterTreeHtml(map) {
    var nodes = (map.nodes || []).filter(function (node) { return !node.unplaced; });
    if (!nodes.length) { return '<p class="small">这一章没有地图坐标。</p>'; }
    if ((map.bounds || {}).pixel) {
      return '<p class="small">这一章的坐标是像素级的（单人 raid 结构），游戏里不是树状地图，请用下面的小节列表。</p>';
    }
    var byId = {};
    nodes.forEach(function (node) { byId[String(node.sectionId)] = node; });
    function px(node) { return (node.x - 1) * MAP_CELL_W + MAP_PAD + MAP_NODE_OFF_X; }
    function py(node) { return (node.y - 1) * MAP_CELL_H + MAP_PAD + MAP_NODE_OFF_Y; }
    var maxX = 0, maxY = 0;
    nodes.forEach(function (node) {
      maxX = Math.max(maxX, px(node) + MAP_NODE_W);
      maxY = Math.max(maxY, py(node) + MAP_NODE_H);
    });
    var edges = '';
    nodes.forEach(function (node) {
      (node.connect || []).forEach(function (targetId) {
        var target = byId[String(targetId)];
        if (!target) { return; }
        edges += '<line x1="' + (px(node) + MAP_NODE_W / 2) + '" y1="' + (py(node) + MAP_NODE_H / 2)
          + '" x2="' + (px(target) + MAP_NODE_W / 2) + '" y2="' + (py(target) + MAP_NODE_H / 2) + '"'
          + (node.cleared ? ' class="on"' : '') + '/>';
      });
    });
    var html = '<div class="lr-map-scroll"><div class="lr-map-canvas" style="width:' + maxX + 'px;height:' + maxY + 'px">'
      + '<svg class="lr-map-edges" width="' + maxX + '" height="' + maxY + '">' + edges + '</svg>';
    nodes.forEach(function (node) {
      var cls = 'lr-map-node' + (node.cleared ? ' lr-clear' : (node.canPlay ? ' lr-new' : ' lr-locked'))
        + (node.required ? ' lr-required' : '');
      var status = node.cleared ? 'CLEAR' : (node.canPlay ? 'NEW' : '未解锁');
      html += '<button type="button" class="' + cls + '" data-tree-node="' + esc(node.sectionId)
        + '" style="left:' + px(node) + 'px;top:' + py(node) + 'px" title="'
        + esc(node.sectionId + ' · ' + (node.questCleared + '/' + node.questCount)) + '">'
        + '<span class="lr-node-label">' + esc(node.title || '') + '</span>'
        + '<span class="lr-node-status">' + esc(status) + '</span></button>';
    });
    return html + '</div></div>';
  }

  function renderChapterMap() {
    return paintCurrent(api('/api/view/chapter-map?chapterId=' + encodeURIComponent(S.chapterMap)), function (map) {
      var html = '<div class="crumbs"><button class="mini" data-chapter-back>章节一览</button>'
        + '<span>›</span><span class="small">' + esc(map.chapterNoForView || '') + ' '
        + esc(map.title || '') + '</span></div>';
      html += '<section class="panel"><h2>' + esc(map.chapterNoForView || ('章 ' + map.chapterId)) + ' '
        + esc(map.title || '') + ' <span class="tag">' + esc(map.chapterType || '') + '</span>'
        + '<span class="spacer"></span><span class="small">小节 ' + esc(map.nodeCount) + '</span></h2>'
        + '<p class="small">节点位置与连线照搬游戏分支地图（LostRecordMap.js：格 66×48、节点 150×94）。'
        + '点节点进入这一节。</p>'
        + '<p class="small policyNote">本章 ' + esc(map.nodeCount) + ' 节，标「必需」的是开出下一章的最小集。'
        + '<b>整章通关</b> = 全部小节；<b>整章解锁</b> = 只补最小集。</p>'
        + '<div class="row"><button class="mini" data-chapter-clear title="把本章所有小节（含不在最小集里的分支）都设为已通关">'
        + (map.hasQuests ? '整章通关（全部）' : '剧情通关（全部）') + '</button>'
        + '<button class="mini" data-chapter-unlock title="只补开出下一章所需的最小集">整章解锁（最小集）</button></div>'
        + (map.hasQuests ? '' : '<p class="small">这一章没有战斗关卡，通关状态只写小节本身（纯剧情）。</p>')
        + (map.hasLayout
          ? '<div class="treeWrap">' + chapterTreeHtml(map) + '</div>'
          : '<p class="small">这一章是线性结构，没有地图坐标，直接看下面的小节列表。</p>')
        + '</section>';
      html += '<section class="panel"><h2>小节列表</h2><table><thead><tr><th>小节</th><th>标题</th>'
        + '<th>任务</th><th>状态</th><th>前置</th></tr></thead><tbody>';
      (map.nodes || []).forEach(function (node) {
        html += '<tr><td class="num">' + esc(node.sectionId) + '</td><td>' + esc(node.title || '') + '</td>'
          + '<td class="num">' + esc(node.questCleared) + ' / ' + esc(node.questCount) + '</td><td>'
          + (node.cleared ? '<span class="tag ok">已通关</span>'
            : (node.canPlay ? '<span class="tag">已解锁</span>' : '<span class="tag warn">未解锁</span>'))
          + '</td><td class="small">' + esc((node.requires || []).join(' / ') || '—') + '</td>'
          + '<td><button class="mini" data-open-section="' + esc(node.sectionId) + '">进入</button></td></tr>';
      });
      html += '</tbody></table></section>';
      $('main').innerHTML = html;

      var back = $('main').querySelector('[data-chapter-back]');
      back.addEventListener('click', function () {
        S.chapterMap = null;
        render();
      });
      $('main').querySelector('[data-chapter-clear]').addEventListener('click', function () {
        edit([{ op: 'chapter.clear', chapterId: map.chapterId, cleared: true }]);
      });
      $('main').querySelector('[data-chapter-unlock]').addEventListener('click', function () {
        edit([{ op: 'chapter.unlock', chapterId: map.chapterId }]);
      });
      $('main').querySelectorAll('[data-tree-node]').forEach(function (node) {
        node.addEventListener('click', function () {
          S.sectionSel = node.getAttribute('data-tree-node');
          render();
        });
      });
      $('main').querySelectorAll('[data-open-section]').forEach(function (button) {
        button.addEventListener('click', function () {
          S.sectionSel = button.getAttribute('data-open-section');
          render();
        });
      });
    });
  }

function renderSectionDetail() {
    return paintCurrent(api('/api/view/section?sectionId=' + encodeURIComponent(S.sectionSel)), function (d) {
      var state = d.cleared ? '已通关' : (d.canPlay ? '已解锁' : '未解锁');
      var html = '<div class="crumbs"><button class="mini" data-section-back>章节</button>'
        + '<span>›</span><span class="small">' + esc(d.title || d.sectionId) + '</span></div>';
      html += '<section class="panel"><h2>' + esc(d.title || ('小节 ' + d.sectionId))
        + ' <span class="tag">' + esc(d.questType || '') + '</span>'
        + '<span class="spacer"></span><span class="small">' + esc(d.sectionId) + '</span></h2>'
        + '<div class="kvList">'
        + '<div class="kv"><span>状态</span><strong>' + esc(state) + '</strong></div>'
        + '<div class="kv"><span>前置小节</span><strong>' + esc((d.requires || []).join(' / ') || '—') + '</strong></div>'
        + '<div class="kv"><span>解锁后开启</span><strong>' + esc((d.unlocks || []).join(' / ') || '—') + '</strong></div>'
        + '</div><div class="row">'
        + '<button class="mini" data-section-unlock title="沿依赖链把必需的前置小节补成通关，不动无关分支">解锁（补前置）</button>'
        + (d.lockable ? '<button class="mini" data-section-lock>锁定（之后全部）</button>'
          : '<span class="small">第一部第一章第一话不可锁定</span>')
        + '<button class="mini" data-section-clear>整话通关</button>'
        + '<button class="mini" data-section-reset>整话回退</button>'
        + '</div></section>';
      if (!(d.quests || []).length) {
        html += '<section class="panel"><h2>剧情</h2>'
          + '<p class="small">这一节是纯剧情，没有战斗关卡；「通关」只写小节自身的 cleared。</p></section>';
      } else {
        html += '<section class="panel"><h2>战斗（' + esc(d.quests.length) + '）</h2>'
          + '<table><thead><tr><th>关卡</th><th>通关</th><th>星级</th><th>操作</th></tr></thead><tbody>';
      (d.quests || []).forEach(function (quest) {
        var stars = (quest.missions || []).map(function (status, index) {
          return '<button class="mini star' + (status === 'CLEARED' ? ' on' : '') + '"'
            + ' data-mission="' + esc(quest.questBattleId) + '" data-index="' + (index + 1) + '"'
            + ' data-on="' + (status === 'CLEARED' ? '1' : '0') + '" title="'
            + esc(status || '') + '">★</button>';
        }).join('');
        html += '<tr><td class="num">' + esc(quest.questBattleId) + '</td><td>'
          + (quest.cleared ? '<span class="tag ok">已通关</span>' : '<span class="tag">未通关</span>')
          + '</td><td>' + stars + '</td><td>'
          + '<button class="mini" data-quest-toggle="' + esc(quest.questBattleId) + '" data-on="'
          + (quest.cleared ? '1' : '0') + '">' + (quest.cleared ? '回退' : '通关') + '</button>'
          + '</td></tr>';
      });
        html += '</tbody></table><p class="small">整话通关会把这一节的所有任务置为已通关且三星；'
          + '星级对应 missionStatus1..3。</p></section>';
      }
      $('main').innerHTML = html;

      var back = $('main').querySelector('[data-section-back]');
      if (back) {
        back.addEventListener('click', function () {
          S.sectionSel = null;
          render();
        });
      }
      var unlock = $('main').querySelector('[data-section-unlock]');
      if (unlock) {
        unlock.addEventListener('click', function () {
          edit([{ op: 'story.unlock', sectionId: d.sectionId }]);
        });
      }
      var lock = $('main').querySelector('[data-section-lock]');
      if (lock) {
        lock.addEventListener('click', function () {
          if (!window.confirm('锁定这一节会把它后面的小节一起设为未解锁、未通关，继续？')) { return; }
          edit([{ op: 'story.lock', sectionId: d.sectionId }]);
        });
      }
      var clear = $('main').querySelector('[data-section-clear]');
      if (clear) {
        clear.addEventListener('click', function () {
          edit([{ op: 'section.clear', sectionId: d.sectionId, cleared: true }]);
        });
      }
      var reset = $('main').querySelector('[data-section-reset]');
      if (reset) {
        reset.addEventListener('click', function () {
          edit([{ op: 'section.clear', sectionId: d.sectionId, cleared: false }]);
        });
      }
      $('main').querySelectorAll('[data-quest-toggle]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'quest.set', questBattleId: Number(button.getAttribute('data-quest-toggle')),
            cleared: button.getAttribute('data-on') !== '1' }]);
        });
      });
      $('main').querySelectorAll('[data-mission]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'quest.mission', questBattleId: Number(button.getAttribute('data-mission')),
            index: Number(button.getAttribute('data-index')),
            status: button.getAttribute('data-on') === '1' ? 'NON_CLEAR' : 'CLEARED' }]);
        });
      });
    });
  }

  function renderChapters() {
    if (S.sectionSel) { return renderSectionDetail(); }
    if (S.chapterMap) { return renderChapterMap(); }
    return paintCurrent(api('/api/view/chapters'), function (data) {
      var html = '<div class="toolbar"><button data-clear>全部章节标记已通</button>'
        + '<button data-reset>全部章节标记未通</button><span class="spacer"></span>'
        + '<span class="small">共 ' + data.total + ' 章</span></div>'
        + '<p class="small policyNote">策略：<b>整章通关</b> 把该章全部小节设为已通关；'
        + '<b>整章解锁</b> 与解锁时的自动补齐只补<b>最小集</b>（开出下一章所需的小节，树上标「必需」）。</p>';
      var groups = [];
      var groupIndex = {};
      data.rows.forEach(function (row) {
        var label = row.partLabel || '其他';
        if (!groupIndex[label]) {
          groupIndex[label] = { label: label, rows: [] };
          groups.push(groupIndex[label]);
        }
        groupIndex[label].rows.push(row);
      });
      groups.forEach(function (group) {
        html += '<section class="panel"><h2>' + esc(group.label)
          + ' <span class="small">' + group.rows.length + ' 章</span></h2>'
          + '<table><thead><tr><th>章节</th><th>标题</th><th>类型</th><th>小节进度</th><th>状态</th><th>操作</th></tr></thead><tbody>';
        group.rows.forEach(function (row) {
          html += '<tr><td>' + esc(row.no || row.chapterId) + '</td><td>' + esc(row.title || '') + '</td>'
            + '<td class="small">' + esc(row.chapterType || row.questType || '')
          + (row.hasQuests ? '' : ' <span class="tag warn">纯剧情</span>') + '</td>'
            + '<td class="num">' + esc(row.clearedSections) + ' / ' + esc(row.sectionCount)
            + (row.requiredCount ? '<span class="small">（必需 ' + esc(row.requiredCleared) + '/'
              + esc(row.requiredCount) + '）</span>' : '') + '</td><td>'
            + '<button class="mini toggle" data-chapter="' + esc(row.chapterId) + '" data-on="'
            + (row.cleared ? '1' : '0') + '">' + (row.cleared ? '已通' : '未通') + '</button></td><td>'
            + '<button class="mini" data-chapter-run="' + esc(row.chapterId) + '">'
            + (row.hasQuests ? '整章通关' : '剧情通关') + '</button> '
            + '<button class="mini" data-chapter-unlock="' + esc(row.chapterId) + '">整章解锁</button>'
            + ' <button class="mini" data-chapter-tree="' + esc(row.chapterId) + '">详情</button>'
            + '</td></tr>';
        });
        html += '</tbody></table></section>';
      });
      $('main').innerHTML = html;
      $('main').querySelectorAll('[data-chapter]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'chapter.set', chapterId: Number(button.getAttribute('data-chapter')),
            cleared: button.getAttribute('data-on') !== '1' }]);
        });
      });
      $('main').querySelectorAll('[data-chapter-run]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'chapter.clear', chapterId: Number(button.getAttribute('data-chapter-run')), cleared: true }]);
        });
      });
      $('main').querySelectorAll('[data-chapter-unlock]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'chapter.unlock', chapterId: Number(button.getAttribute('data-chapter-unlock')) }]);
        });
      });
      $('main').querySelectorAll('[data-chapter-tree]').forEach(function (button) {
        button.addEventListener('click', function () {
          S.chapterMap = String(button.getAttribute('data-chapter-tree'));
          render();
        });
      });
      $('main').querySelector('[data-clear]').addEventListener('click', function () {
        edit([{ op: 'chapter.bulk', cleared: true }]);
      });
      $('main').querySelector('[data-reset]').addEventListener('click', function () {
        edit([{ op: 'chapter.bulk', cleared: false }]);
      });
    });
  }

  // ------------------------------------------------------------ 挑战

  function renderChallenges() {
    return paintCurrent(api('/api/view/challenges'), function (data) {
      var html = '<p class="small">目标次数取自官方条件文案（如「通关剧情3次」）；没有条件文案的 '
        + data.hiddenUnknown + ' 条不显示；' + data.withoutTarget
        + ' 条官方未提供目标次数，目标列显示「—」（编辑器不从文案推测）。已达成是累计次数，不是开关。</p>';
      data.groups.forEach(function (group) {
        html += '<section class="panel"><h2>' + esc(group.label) + ' <span class="small">（'
          + group.total + ' 条 · ' + esc(group.table) + '）</span></h2>'
          + '<div class="toolbar"><button data-fill="' + esc(group.table) + '">按目标补满</button>'
          + '<button data-zero="' + esc(group.table) + '">全部清零</button></div>'
          + '<table><thead><tr><th>条件</th><th>目标</th><th>已达成</th><th>领取时间</th></tr></thead><tbody>';
        group.rows.forEach(function (row) {
          html += '<tr><td><b>' + esc(nl(row.label)) + '</b> <span class="small">#'
            + esc(row.challengeId) + '</span></td>'
            + '<td class="num">' + (row.target === null || row.target === undefined ? '—' : row.target) + '</td>'
            + '<td><input data-ch type="number" min="0" value="' + esc(row.clearedCount) + '"> '
            + '<button class="mini" data-apply-ch="' + esc(group.table) + '" data-id="'
            + esc(row.challengeId) + '">应用</button></td>'
            + '<td class="small">' + esc(row.receivedAt || '') + '</td></tr>';
        });
        html += '</tbody></table></section>';
      });
      $('main').innerHTML = html;
      $('main').querySelectorAll('[data-apply-ch]').forEach(function (button) {
        button.addEventListener('click', function () {
          var input = button.closest('tr').querySelector('[data-ch]');
          var value = readInteger(input);
          if (value === null) { return; }
          edit([{ op: 'challenge.set', table: button.getAttribute('data-apply-ch'),
            challengeId: Number(button.getAttribute('data-id')), clearedCount: value }]);
        });
      });
      $('main').querySelectorAll('[data-fill]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'challenge.clear', table: button.getAttribute('data-fill'),
            clearedCount: 1, byTarget: true }]);
        });
      });
      $('main').querySelectorAll('[data-zero]').forEach(function (button) {
        button.addEventListener('click', function () {
          edit([{ op: 'challenge.clear', table: button.getAttribute('data-zero'), clearedCount: 0 }]);
        });
      });
    });
  }

  // ------------------------------------------------------------ 扭蛋

  function renderGacha() {
    return paintCurrent(api('/api/view/gacha'), function (data) {
      var html = '<section class="panel"><h2>扭蛋计数（userGachaKindList）</h2>'
        + '<div class="toolbar"><button data-reset>全部 kinds 总次数清零</button>'
        + '<span class="spacer"></span><span class="small">每行可单独设置总次数</span></div>'
        + '<table><thead><tr><th>gachaKindId</th><th>总次数</th><th>日</th><th>周</th><th>月</th><th>最近</th></tr></thead><tbody>';
      data.kinds.forEach(function (row) {
        html += '<tr><td><b>' + esc(row.label || '扭蛋种类') + '</b> <span class="small">#'
          + esc(row.gachaKindId) + '</span></td>'
          + '<td><input data-gk="' + esc(row.gachaKindId) + '" type="number" min="0" value="'
          + esc(row.totalCount) + '"> <button class="mini" data-apply-gk="' + esc(row.gachaKindId)
          + '">应用</button></td>'
          + '<td class="num">' + fmt(row.dailyCount) + '</td><td class="num">' + fmt(row.weeklyCount)
          + '</td><td class="num">' + fmt(row.monthlyCount) + '</td><td class="small">'
          + esc(row.recentGachaAt || '') + '</td></tr>';
      });
      html += '</tbody></table></section>';
      html += '<section class="panel"><h2>扭蛋组（userGachaGroupList）</h2>'
        + '<table><thead><tr><th>gachaGroupId</th><th>count</th><th>paid</th><th>总次数</th><th>日</th><th>最近</th></tr></thead><tbody>';
      data.groups.forEach(function (row) {
        html += '<tr><td><b>' + esc(row.label || '扭蛋组') + '</b> <span class="small">#'
          + esc(row.gachaGroupId) + '</span></td>'
          + '<td><input data-gg="' + esc(row.gachaGroupId) + '" type="number" min="0" value="'
          + esc(row.count) + '"> <button class="mini" data-apply-gg="' + esc(row.gachaGroupId)
          + '">应用</button></td>'
          + '<td class="num">' + fmt(row.paid) + '</td><td class="num">' + fmt(row.totalCount)
          + '</td><td class="num">' + fmt(row.dailyCount) + '</td><td class="small">'
          + esc(row.recentGachaAt || '') + '</td></tr>';
      });
      html += '</tbody></table></section>';
      html += '<p class="small">扭蛋回执：' + fmt(data.receiptCount) + ' 条</p>';
      $('main').innerHTML = html;
      $('main').querySelectorAll('[data-apply-gk]').forEach(function (button) {
        button.addEventListener('click', function () {
          var input = button.closest('tr').querySelector('[data-gk]');
          var value = readInteger(input);
          if (value === null) { return; }
          edit([{ op: 'gacha.set', field: 'totalCount', value: value,
            gachaKindId: Number(button.getAttribute('data-apply-gk')) }]);
        });
      });
      $('main').querySelectorAll('[data-apply-gg]').forEach(function (button) {
        button.addEventListener('click', function () {
          var input = button.closest('tr').querySelector('[data-gg]');
          var value = readInteger(input);
          if (value === null) { return; }
          edit([{ op: 'gacha.set', field: 'count', value: value,
            gachaGroupId: Number(button.getAttribute('data-apply-gg')) }]);
        });
      });
      $('main').querySelector('[data-reset]').addEventListener('click', function () {
        edit([{ op: 'gacha.set', field: 'totalCount', value: 0, all: true }]);
      });
    });
  }

  // ------------------------------------------------------------ 养成素材（gift）

  function renderGifts() {
    return paintCurrent(api('/api/view/gifts'), function (data) {
      var groups = data.groups || [];
      if (!groups.length) {
        $('main').innerHTML = '<div class="empty">没有养成素材</div>';
        return;
      }
      var active = S.giftTab;
      if (!active || !groups.some(function (group) { return group.label === active; })) {
        active = groups[0].label;
      }
      S.giftTab = active;
      var html = '<div class="toolbar"><input type="text" data-search placeholder="搜索素材名称或 giftId">'
        + '<span class="spacer"></span><span class="small">' + data.total + ' 种 · ' + groups.length
        + ' 类（未拥有显示 0）</span></div><div class="subtabs">';
      groups.forEach(function (group) {
        html += '<button class="subtab' + (group.label === active ? ' active' : '') + '" data-gift-tab="'
          + esc(group.label) + '">' + esc(group.label) + ' <span class="small">' + group.total
          + '</span></button>';
      });
      html += '</div>';
      var current = groups.filter(function (group) { return group.label === active; })[0];
      html += '<div class="grid">';
      current.rows.forEach(function (row) {
        html += tileHtml(
          thumb(row.icon, row.name, 'icon'),
          row.name,
          '#' + row.giftId,
          '<div class="row"><input data-gift type="number" min="0" value="' + esc(row.quantity) + '">'
            + '<button class="mini" data-save>应用</button></div>'
            + statLine([['累计', row.total]]),
          (row.owned ? '<span class="tag ok">已拥有</span>' : '<span class="tag">未拥有</span>')
        );
      });
      html += '</div>';
      $('main').innerHTML = html;
      $('main').querySelectorAll('[data-gift-tab]').forEach(function (button) {
        button.addEventListener('click', function () {
          S.giftTab = button.getAttribute('data-gift-tab');
          render();
        });
      });
      var search = $('main').querySelector('[data-search]');
      var timer = null;
      search.addEventListener('input', function () {
        if (timer) { clearTimeout(timer); }
        timer = setTimeout(function () {
          var needle = search.value.trim().toLowerCase();
          $('main').querySelectorAll('.grid .tile').forEach(function (tile) {
            var text = tile.textContent.toLowerCase();
            tile.style.display = !needle || text.indexOf(needle) >= 0 ? '' : 'none';
          });
        }, 200);
      });
      var rows = current.rows;
      $('main').querySelectorAll('[data-save]').forEach(function (button, index) {
        button.addEventListener('click', function () {
          var input = button.closest('.tile').querySelector('[data-gift]');
          var value = readInteger(input);
          if (value === null) { return; }
          edit([{ op: 'gift.set', giftId: rows[index].giftId, quantity: value }]);
        });
      });
    });
  }

  function openGiftPicker() {
    openPicker({
      title: '添加养成素材',
      placeholder: '搜索素材名称或 giftId',
      load: function (q) { return api('/api/view/gifts-catalog?limit=120&q=' + encodeURIComponent(q)); },
      meta: function (data) { return '匹配 ' + data.rows.length + ' 条'; },
      render: function (rows) {
        var html = '<div class="grid">';
        rows.forEach(function (row) {
          html += tileHtml(
            thumb(row.icon, row.name, 'icon'),
            row.name,
            '#' + row.giftId,
            '<div class="row"><input data-qty type="number" min="0" value="1">'
              + '<button class="mini" data-add>添加</button></div>'
          );
        });
        return html + '</div>';
      },
      bind: function (host, rows) {
        host.querySelectorAll('[data-add]').forEach(function (button, index) {
          button.addEventListener('click', function (event) {
            var input = button.closest('.tile').querySelector('[data-qty]');
            var value = readInteger(input);
            if (value === null) { event.preventDefault(); return; }
            edit([{ op: 'gift.set', giftId: rows[index].giftId, quantity: value }]);
          });
        });
      }
    });
  }

  // ------------------------------------------------------------ 原始 JSON

  function renderRaw() {
    var epoch = renderEpoch, latestRead = 0;
    function showRead(path, draw) {
      var read = ++latestRead;
      var output = $('rawOut');
      api(path).then(function (data) {
        if (epoch === renderEpoch && read === latestRead) { draw(output, data); }
      }, function (error) {
        if (epoch === renderEpoch && read === latestRead) { output.textContent = '读取失败：' + error.message; }
      });
    }
    $('main').innerHTML = '<section class="panel"><h2>按路径读写</h2>'
      + '<p class="small">路径语法：state.gameUser.riche · state.userItemList[itemId=YELL].quantity · '
      + 'state.userQuestBattleList[questBattleId=10110151].cleared。类型必须与原值一致，否则拒绝写入。</p>'
      + '<div class="toolbar"><input type="text" id="rawPath" placeholder="state.gameUser.riche" style="max-width:520px">'
      + '<button id="rawRead">读取</button><button id="rawPreview" class="ghost">整档预览</button></div>'
      + '<div class="toolbar"><input type="text" id="rawValue" placeholder="新值，例如 99999 / \"text\" / true" style="max-width:520px">'
      + '<button id="rawWrite" class="primary">写入</button></div>'
      + '<pre class="json" id="rawOut">（读取结果显示在这里）</pre></section>';
    $('rawRead').addEventListener('click', function () {
      var path = $('rawPath').value.trim();
      if (!path) { return; }
      showRead('/api/raw?path=' + encodeURIComponent(path), function (output, data) {
        output.textContent = JSON.stringify(data.value, null, 2);
      });
    });
    $('rawWrite').addEventListener('click', function () {
      var path = $('rawPath').value.trim();
      var text = $('rawValue').value.trim();
      if (!path || !text) { fail(new Error('需要同时填写路径和值')); return; }
      var value;
      try { value = JSON.parse(text); } catch (e) { value = text; }
      edit([{ op: 'raw.set', path: path, value: value }]);
    });
    $('rawPreview').addEventListener('click', function () {
      showRead('/api/raw', function (output, data) {
        var text = JSON.stringify(data.value, null, 1);
        output.textContent = text.length > 200000 ? text.slice(0, 200000) + '\n…（截断）' : text;
      });
    });
  }

  // ------------------------------------------------------------ 打开 / 保存

  function openArchive(path) {
    return api('/api/status').then(function (status) {
      if (status.dirty && !window.confirm('当前存档「' + status.summary.label + '」有未保存的改动。放弃改动并打开所选存档？')) { return; }
      return api('/api/open', { path: path }).then(function (payload) {
        S.page = {};
        toast('已打开 ' + payload.summary.label, 'ok');
        return refreshStatus().then(function () { renderTabs(); render(); });
      });
    }).catch(fail);
  }

  function saveAs() {
    var summary = (S.status && S.status.summary) || null;
    if (!summary) { toast('请先打开一份存档', 'err'); return; }
    var stem = String(summary.path).split(/[\\/]/).pop().replace(/\.zip$/i, '') || 'magia-save';
    var suggestion = stem + '-edited-' + Date.now() + '.zip';
    function manualPath() { return window.prompt('另存为（不会覆盖原存档）', suggestion); }
    api('/api/pick', { mode: 'save', initial: summary.path, suggest: suggestion })
      .then(function (payload) {
        if (payload.path) { return payload.path; }
        return payload.available === false ? manualPath() : null;
      }, manualPath)
      .then(function (path) {
        if (!path) { return null; }
        var label = window.prompt('存档标签（≤80 字符）', summary ? summary.label : 'edited');
        if (label === null) { return null; }
        return api('/api/save', { path: path, label: label }).then(function (payload) {
          var saved = payload.saved;
          toast('已写出 ' + saved.path + '（revision ' + saved.revision + '，' + saved.parts + ' 片）', 'ok');
          $('openPath').value = saved.path;
          return refreshStatus().then(function () { return Promise.resolve(render()); });
        });
      })
      .catch(fail);
  }

  function boot() {
    renderTabs();
    // 弹窗的三个关闭入口：右上角按钮、点击遮罩、Esc。
    $('modalClose').addEventListener('click', closeModal);
    $('modal').addEventListener('click', function (event) {
      if (event.target === $('modal')) { closeModal(); }
    });
    document.addEventListener('keydown', dialogKeys);
    // 选中条目后立即收起选择器（委托一次，picker 重开不会叠加监听）。
    $('modalBody').addEventListener('click', function (event) {
      var target = event.target;
      if (!event.defaultPrevented && target && target.closest && target.closest('[data-add], [data-add-chara]')) { closeModal(); }
    });

    $('btnPick').addEventListener('click', function () {
      api('/api/pick', { mode: 'open', initial: $('openPath').value.trim() }).then(function (payload) {
        if (payload.path) { $('openPath').value = payload.path; }
        else if (!payload.available) { toast('这台机器没有可用的文件对话框，请手动填路径', 'err'); }
      }).catch(fail);
    });
    $('btnOpen').addEventListener('click', function () {
      var path = $('openPath').value.trim();
      if (!path) { fail(new Error('请输入存档路径')); return; }
      openArchive(path);
    });
    $('btnSave').addEventListener('click', saveAs);
    $('btnUndo').addEventListener('click', function () {
      api('/api/undo', {}).then(function () { toast('已撤销一步', 'ok'); return refreshStatus().then(render); }).catch(fail);
    });
    $('btnDiscard').addEventListener('click', function () {
      if (!window.confirm('丢弃全部未保存改动？')) { return; }
      api('/api/discard', {}).then(function () {
        toast('已回到磁盘上的存档状态', 'ok');
        return refreshStatus().then(render);
      }).catch(fail);
    });
    window.addEventListener('hashchange', function () {
      if (suppressHash) { suppressHash = false; return; }
      applyHash();
    });
    refreshStatus().then(function (status) {
      if (status.loaded) {
        $('openPath').value = status.archive || '';
        applyHash();
      }
    }).catch(fail);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
