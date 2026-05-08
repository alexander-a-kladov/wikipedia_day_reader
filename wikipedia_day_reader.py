#!/usr/bin/env python3
"""
Wikipedia Day Reader
Reads Wikipedia's "Month Day" article and categorizes Events and Births.
"""

import json
import re
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.request
import urllib.error

DEFAULT_CONFIG = {
    "event_categories": [
        {"id": "science", "label": "Наука", "keywords": ["science", "physics", "chemistry", "biology", "mathematics", "astronomy", "medicine", "discovery", "experiment", "laboratory", "university founded", "institute", "research", "telescope", "satellite", "space", "launch", "nuclear", "vaccine", "theory", "equation", "element", "atom", "DNA", "genome", "invention", "patent"]},
        {"id": "art", "label": "Искусство", "keywords": ["art", "painting", "sculpture", "museum", "gallery", "exhibition", "artist", "opera", "theatre", "theater", "ballet", "symphony", "premiere", "film", "cinema", "architecture", "architect", "design", "photography", "novel", "poem", "play", "concert", "music", "compose", "canvas", "masterpiece"]},
        {"id": "education", "label": "Образование", "keywords": ["university", "college", "school", "academy", "institute", "founded", "established", "education", "library", "learning", "faculty", "campus", "degree", "diploma", "scholarship"]},
        {"id": "literature", "label": "Литература", "keywords": ["author", "writer", "poet", "novelist", "book", "novel", "poem", "poetry", "published", "literature", "literary", "story", "tale", "essay", "manuscript", "biography", "fiction", "prose"]}
    ],
    "birth_categories": [
        {"id": "scientists", "label": "Учёные", "keywords": ["physicist", "chemist", "biologist", "mathematician", "astronomer", "scientist", "researcher", "geologist", "botanist", "zoologist", "physician", "doctor", "engineer", "computer scientist", "neuroscientist", "archaeologist", "paleontologist", "oceanographer", "meteorologist", "statistician"]},
        {"id": "artists", "label": "Художники", "keywords": ["painter", "sculptor", "artist", "illustrator", "printmaker", "photographer", "graphic", "engraver", "muralist", "ceramist", "craftsman", "watercolorist", "draughtsman"]},
        {"id": "composers", "label": "Композиторы", "keywords": ["composer", "musician", "pianist", "violinist", "cellist", "conductor", "organist", "singer", "soprano", "tenor", "baritone", "guitarist", "drummer", "songwriter", "instrumentalist", "opera"]},
        {"id": "inventors", "label": "Изобретатели", "keywords": ["inventor", "engineer", "technologist", "developer", "innovator", "pioneer", "designer", "entrepreneur", "industrialist"]}
    ],
    "russian_keywords": ["russian", "soviet", "russia", "ussr", "moscow", "saint petersburg", "leningrad", "petrograd", "romanov", "ukraine", "ukrainian", "belarusian", "kazakh", "georgian", "armenian", "azerbaijani"]
}


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Читатель Дней Истории</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=Source+Serif+4:ital,opsz,wght@0,8..60,300;0,8..60,400;1,8..60,300&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #faf8f4;
    --surface: #fff;
    --surface2: #f5f2ec;
    --accent: #7c4f2a;
    --accent2: #c17f3b;
    --text: #1e1a14;
    --text2: #5c5244;
    --border: #ddd8ce;
    --science: #2d5986;
    --art: #8b3a62;
    --edu: #2d7a4a;
    --lit: #6b4c8b;
    --sci2: #4a6b36;
    --art2: #8b5e2d;
    --comp: #2d6b7a;
    --inv: #7a4a2d;
    --russian: #8b2d2d;
    --tag-r: 6px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Source Serif 4', Georgia, serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  header {
    background: var(--accent);
    color: #fff;
    padding: 18px 32px;
    display: flex;
    align-items: center;
    gap: 20px;
    border-bottom: 3px solid var(--accent2);
  }
  header h1 {
    font-family: 'Playfair Display', serif;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }
  .subtitle {
    font-size: 13px;
    opacity: 0.75;
    margin-top: 2px;
  }
  .controls {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 32px;
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  input[type=date] {
    font-family: 'Source Serif 4', serif;
    font-size: 15px;
    padding: 7px 12px;
    border: 1.5px solid var(--border);
    border-radius: var(--tag-r);
    background: var(--bg);
    color: var(--text);
    cursor: pointer;
  }
  button {
    font-family: 'Source Serif 4', serif;
    font-size: 14px;
    padding: 7px 18px;
    border-radius: var(--tag-r);
    border: 1.5px solid;
    cursor: pointer;
    transition: all .15s;
    font-weight: 400;
  }
  .btn-primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  .btn-primary:hover { background: var(--accent2); border-color: var(--accent2); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-secondary {
    background: transparent;
    border-color: var(--border);
    color: var(--text2);
  }
  .btn-secondary:hover { border-color: var(--accent2); color: var(--accent); }
  .main {
    display: grid;
    grid-template-columns: 260px 1fr;
    gap: 0;
    min-height: calc(100vh - 120px);
  }
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    padding: 20px 16px;
    overflow-y: auto;
  }
  .sidebar-section {
    margin-bottom: 24px;
  }
  .sidebar-title {
    font-family: 'Playfair Display', serif;
    font-size: 13px;
    font-weight: 600;
    color: var(--text2);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }
  .cat-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 6px;
    border-radius: 4px;
    margin-bottom: 2px;
    cursor: pointer;
    transition: background .1s;
  }
  .cat-item:hover { background: var(--surface2); }
  .cat-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .cat-label { font-size: 14px; flex: 1; }
  .cat-count {
    font-size: 11px;
    color: var(--text2);
    background: var(--surface2);
    padding: 1px 6px;
    border-radius: 10px;
  }
  .cat-toggle {
    width: 14px; height: 14px;
    border: 1.5px solid var(--border);
    border-radius: 3px;
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 10px;
  }
  .cat-toggle.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .content {
    padding: 28px 36px;
    overflow-y: auto;
  }
  .welcome {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 400px;
    text-align: center;
    color: var(--text2);
  }
  .welcome-icon { font-size: 48px; margin-bottom: 16px; }
  .welcome h2 {
    font-family: 'Playfair Display', serif;
    font-size: 22px;
    color: var(--accent);
    margin-bottom: 8px;
  }
  .loading {
    display: flex; align-items: center; gap: 12px;
    padding: 32px; color: var(--text2);
    font-style: italic; font-size: 16px;
  }
  .spinner {
    width: 22px; height: 22px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .section-block {
    margin-bottom: 32px;
  }
  .section-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
    padding-bottom: 8px;
    border-bottom: 2px solid;
  }
  .section-icon {
    width: 28px; height: 28px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
    color: #fff;
    flex-shrink: 0;
  }
  .section-title {
    font-family: 'Playfair Display', serif;
    font-size: 18px;
    font-weight: 600;
  }
  .section-subtitle {
    font-size: 12px;
    color: var(--text2);
    margin-left: auto;
  }
  .entry {
    padding: 8px 12px;
    border-left: 3px solid transparent;
    margin-bottom: 4px;
    border-radius: 0 4px 4px 0;
    transition: background .1s;
    font-size: 15px;
    line-height: 1.6;
  }
  .entry:hover { background: var(--surface2); }
  .entry a {
    color: var(--accent);
    text-decoration: none;
  }
  .entry a:hover { text-decoration: underline; }
  .year-tag {
    font-family: 'Playfair Display', serif;
    font-weight: 600;
    color: var(--text2);
    font-size: 14px;
    margin-right: 6px;
  }
  .russian-badge {
    display: inline-block;
    font-size: 10px;
    padding: 1px 6px;
    background: #fce8e8;
    color: var(--russian);
    border-radius: 10px;
    border: 1px solid #f5c0c0;
    margin-left: 6px;
    vertical-align: middle;
  }
  .group-header {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px;
    margin-bottom: 6px;
    margin-top: 12px;
  }
  .group-line {
    flex: 1;
    height: 1px;
    background: var(--border);
  }
  .group-label {
    font-size: 12px;
    color: var(--text2);
    font-weight: 600;
    letter-spacing: 0.5px;
    white-space: nowrap;
  }
  .empty-section {
    color: var(--text2);
    font-style: italic;
    font-size: 14px;
    padding: 8px 12px;
  }
  /* Settings modal */
  .modal-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.4);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface);
    border-radius: 10px;
    padding: 28px;
    width: 680px;
    max-width: 95vw;
    max-height: 85vh;
    overflow-y: auto;
    box-shadow: 0 12px 40px rgba(0,0,0,0.2);
  }
  .modal h2 {
    font-family: 'Playfair Display', serif;
    font-size: 20px;
    margin-bottom: 20px;
    color: var(--accent);
  }
  .modal-tabs {
    display: flex; gap: 4px;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0;
  }
  .modal-tab {
    padding: 6px 16px 8px;
    font-size: 14px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    color: var(--text2);
    transition: all .1s;
  }
  .modal-tab.active { border-bottom-color: var(--accent); color: var(--accent); font-weight: 600; }
  .cat-editor { display: none; }
  .cat-editor.active { display: block; }
  .cat-row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 8px;
    background: var(--bg);
  }
  .cat-row input[type=text] {
    font-family: 'Source Serif 4', serif;
    font-size: 13px;
    padding: 5px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--surface);
  }
  .cat-row .label-input { width: 130px; }
  .cat-row .kw-input { flex: 1; font-size: 12px; }
  .cat-row .del-btn {
    padding: 5px 8px;
    font-size: 12px;
    color: #c0392b;
    border-color: #f5c0c0;
    background: #fce8e8;
  }
  .add-cat-btn {
    font-size: 13px;
    padding: 5px 14px;
    margin-top: 8px;
  }
  .modal-actions {
    display: flex; gap: 10px;
    justify-content: flex-end;
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }
  .error-msg {
    background: #fce8e8;
    color: #8b2d2d;
    padding: 12px 16px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 14px;
    border: 1px solid #f5c0c0;
  }
  .page-title {
    font-family: 'Playfair Display', serif;
    font-size: 26px;
    color: var(--accent);
    margin-bottom: 6px;
  }
  .page-meta {
    font-size: 13px;
    color: var(--text2);
    margin-bottom: 24px;
  }
  .tabs {
    display: flex; gap: 4px;
    margin-bottom: 24px;
  }
  .tab {
    padding: 7px 18px;
    font-size: 14px;
    cursor: pointer;
    border-radius: var(--tag-r);
    border: 1.5px solid var(--border);
    color: var(--text2);
    font-family: 'Source Serif 4', serif;
    transition: all .15s;
    background: var(--surface);
  }
  .tab.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
</style>
</head>
<body>
<header>
  <div>
    <h1>📖 Читатель Дней Истории</h1>
    <div class="subtitle">История каждого дня по данным Википедии</div>
  </div>
</header>
<div class="controls">
  <input type="date" id="datePicker" />
  <button class="btn-primary" id="loadBtn" onclick="loadData()">Загрузить</button>
  <button class="btn-secondary" onclick="openSettings()">⚙ Настроить категории</button>
</div>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-section">
      <div class="sidebar-title">Фильтры — События</div>
      <div id="eventFilters"></div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-title">Фильтры — Рождения</div>
      <div id="birthFilters"></div>
    </div>
  </div>
  <div class="content" id="content">
    <div class="welcome">
      <div class="welcome-icon">📅</div>
      <h2>Выберите дату</h2>
      <p>Укажите дату и нажмите «Загрузить», чтобы узнать,<br>что произошло в этот день в истории.</p>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div class="modal-overlay" id="modalOverlay" onclick="closeModalOutside(event)">
  <div class="modal">
    <h2>⚙ Настройка категорий</h2>
    <div class="modal-tabs">
      <div class="modal-tab active" onclick="switchModalTab('events')">События</div>
      <div class="modal-tab" onclick="switchModalTab('births')">Рождения</div>
    </div>
    <div class="cat-editor active" id="eventsEditor"></div>
    <div class="cat-editor" id="birthsEditor"></div>
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeSettings()">Отмена</button>
      <button class="btn-primary" onclick="saveSettings()">Сохранить</button>
    </div>
  </div>
</div>

<script>
let config = null;
let currentData = null;
let activeFilters = { events: {}, births: {} };
let modalTab = 'events';

const COLORS = {
  events: ['#2d5986','#8b3a62','#2d7a4a','#6b4c8b','#7a6b2d','#2d6b7a'],
  births: ['#4a6b36','#8b5e2d','#2d6b7a','#7a4a2d','#8b2d2d','#5e2d8b']
};
const ICONS = {
  events: ['🔬','🎨','🏫','📚','⚙','🌍'],
  births: ['🔬','🎨','🎵','⚙','🏳️','🌍']
};

async function init() {
  const d = new Date();
  document.getElementById('datePicker').value = d.toISOString().split('T')[0];
  const r = await fetch('/api/config');
  config = await r.json();
  renderFilters();
}

function renderFilters() {
  const ef = document.getElementById('eventFilters');
  const bf = document.getElementById('birthFilters');
  ef.innerHTML = '';
  bf.innerHTML = '';

  config.event_categories.forEach((cat, i) => {
    if (!(cat.id in activeFilters.events)) activeFilters.events[cat.id] = true;
    const col = COLORS.events[i % COLORS.events.length];
    ef.appendChild(makeCatItem(cat, col, 'events', i));
  });

  // Russian always first
  const russianItem = { id: 'russians', label: 'Русские / Советские' };
  if (!('russians' in activeFilters.births)) activeFilters.births['russians'] = true;
  bf.appendChild(makeCatItem(russianItem, '#8b2d2d', 'births', -1));

  config.birth_categories.forEach((cat, i) => {
    if (!(cat.id in activeFilters.births)) activeFilters.births[cat.id] = true;
    const col = COLORS.births[i % COLORS.births.length];
    bf.appendChild(makeCatItem(cat, col, 'births', i));
  });
}

function makeCatItem(cat, color, type, idx) {
  const div = document.createElement('div');
  div.className = 'cat-item';
  div.id = 'filter_' + type + '_' + cat.id;
  const on = activeFilters[type][cat.id];
  div.innerHTML = `
    <div class="cat-dot" style="background:${color}"></div>
    <div class="cat-label">${cat.label}</div>
    <span class="cat-count" id="count_${type}_${cat.id}">—</span>
    <div class="cat-toggle ${on?'on':''}" id="toggle_${type}_${cat.id}">${on?'✓':''}</div>`;
  div.onclick = () => toggleFilter(type, cat.id);
  return div;
}

function toggleFilter(type, id) {
  activeFilters[type][id] = !activeFilters[type][id];
  const t = document.getElementById('toggle_' + type + '_' + id);
  if (activeFilters[type][id]) { t.className = 'cat-toggle on'; t.textContent = '✓'; }
  else { t.className = 'cat-toggle'; t.textContent = ''; }
  if (currentData) renderContent(currentData);
}

async function loadData() {
  const date = document.getElementById('datePicker').value;
  if (!date) return;
  const btn = document.getElementById('loadBtn');
  btn.disabled = true;
  btn.textContent = 'Загрузка...';
  document.getElementById('content').innerHTML = '<div class="loading"><div class="spinner"></div>Загружаем данные из Википедии...</div>';
  try {
    const r = await fetch('/api/data?date=' + date);
    const data = await r.json();
    if (data.error) {
      document.getElementById('content').innerHTML = `<div class="error-msg">Ошибка: ${data.error}</div>`;
    } else {
      currentData = data;
      updateCounts(data);
      renderContent(data);
    }
  } catch(e) {
    document.getElementById('content').innerHTML = `<div class="error-msg">Ошибка подключения: ${e.message}</div>`;
  }
  btn.disabled = false;
  btn.textContent = 'Загрузить';
}

function updateCounts(data) {
  data.events.forEach(cat => {
    const el = document.getElementById('count_events_' + cat.id);
    if (el) el.textContent = cat.entries.length;
  });
  const russEl = document.getElementById('count_births_russians');
  if (russEl && data.births_russian) russEl.textContent = data.births_russian.length;
  data.births.forEach(cat => {
    const el = document.getElementById('count_births_' + cat.id);
    if (el) el.textContent = cat.entries.length;
  });
}

let activeTab = 'events';

function renderContent(data) {
  const d = new Date(document.getElementById('datePicker').value + 'T00:00:00');
  const months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
  const dateStr = d.getDate() + ' ' + months[d.getMonth()];

  let html = `<div class="page-title">${dateStr}</div>
    <div class="page-meta">Источник: <a href="${data.wiki_url}" target="_blank" style="color:var(--accent)">Wikipedia — ${data.wiki_title}</a></div>
    <div class="tabs">
      <div class="tab ${activeTab==='events'?'active':''}" onclick="switchTab('events')">📅 События</div>
      <div class="tab ${activeTab==='births'?'active':''}" onclick="switchTab('births')">👤 Рождения</div>
    </div>
    <div class="tab-content ${activeTab==='events'?'active':''}" id="tabEvents">`;

  data.events.forEach((cat, i) => {
    if (!activeFilters.events[cat.id]) return;
    const color = COLORS.events[i % COLORS.events.length];
    html += renderSection(cat, color, ICONS.events[i % ICONS.events.length], 'events', false, []);
  });

  html += `</div><div class="tab-content ${activeTab==='births'?'active':''}" id="tabBirths">`;

  // Russian first
  if (activeFilters.births['russians'] && data.births_russian && data.births_russian.length) {
    html += `<div class="section-block">
      <div class="section-header" style="border-color:#8b2d2d">
        <div class="section-icon" style="background:#8b2d2d">🏳️</div>
        <div class="section-title" style="color:#8b2d2d">Русские и Советские</div>
        <div class="section-subtitle">${data.births_russian.length} чел.</div>
      </div>`;
    data.births_russian.forEach(e => {
      html += `<div class="entry" style="border-left-color:#8b2d2d">${e}</div>`;
    });
    html += `</div>`;
  }

  data.births.forEach((cat, i) => {
    if (!activeFilters.births[cat.id]) return;
    const color = COLORS.births[i % COLORS.births.length];
    html += renderSection(cat, color, ICONS.births[i % ICONS.births.length], 'births', true, data.births_russian || []);
  });

  html += `</div>`;
  document.getElementById('content').innerHTML = html;
}

function renderSection(cat, color, icon, type, markRussian, russianEntries) {
  if (!cat.entries || cat.entries.length === 0) {
    return `<div class="section-block">
      <div class="section-header" style="border-color:${color}">
        <div class="section-icon" style="background:${color}">${icon}</div>
        <div class="section-title" style="color:${color}">${cat.label}</div>
        <div class="section-subtitle">нет записей</div>
      </div>
      <div class="empty-section">Записей по этой категории не найдено</div>
    </div>`;
  }

  const russSet = new Set(russianEntries);
  let html = `<div class="section-block">
    <div class="section-header" style="border-color:${color}">
      <div class="section-icon" style="background:${color}">${icon}</div>
      <div class="section-title" style="color:${color}">${cat.label}</div>
      <div class="section-subtitle">${cat.entries.length} записей</div>
    </div>`;

  cat.entries.forEach(e => {
    const isRu = markRussian && russSet.has(e);
    html += `<div class="entry" style="border-left-color:${color}">${e}${isRu ? '<span class="russian-badge">🇷🇺 рус.</span>' : ''}</div>`;
  });
  html += `</div>`;
  return html;
}

function switchTab(tab) {
  activeTab = tab;
  if (currentData) renderContent(currentData);
}

// Settings modal
function openSettings() {
  renderModalEditors();
  document.getElementById('modalOverlay').classList.add('open');
}

function closeSettings() {
  document.getElementById('modalOverlay').classList.remove('open');
}

function closeModalOutside(e) {
  if (e.target === document.getElementById('modalOverlay')) closeSettings();
}

function switchModalTab(tab) {
  modalTab = tab;
  document.querySelectorAll('.modal-tab').forEach((t,i) => {
    t.classList.toggle('active', (i===0&&tab==='events')||(i===1&&tab==='births'));
  });
  document.getElementById('eventsEditor').classList.toggle('active', tab==='events');
  document.getElementById('birthsEditor').classList.toggle('active', tab==='births');
}

function renderModalEditors() {
  renderEditor('eventsEditor', config.event_categories, 'events');
  renderEditor('birthsEditor', config.birth_categories, 'births');
}

function renderEditor(containerId, cats, type) {
  const c = document.getElementById(containerId);
  c.innerHTML = `<p style="font-size:13px;color:var(--text2);margin-bottom:12px">Ключевые слова разделяются запятой. По ним определяется принадлежность события к категории.</p>`;
  cats.forEach((cat, i) => {
    c.innerHTML += `<div class="cat-row">
      <input type="text" class="label-input" value="${cat.label}" placeholder="Название" id="${type}_label_${i}">
      <input type="text" class="kw-input" value="${cat.keywords.join(', ')}" placeholder="ключевые слова через запятую" id="${type}_kw_${i}">
      <button class="btn-secondary del-btn" onclick="removeCat('${type}', ${i})">✕</button>
    </div>`;
  });
  c.innerHTML += `<button class="btn-secondary add-cat-btn" onclick="addCat('${type}')">+ Добавить категорию</button>`;
}

function removeCat(type, idx) {
  const cats = type === 'events' ? config.event_categories : config.birth_categories;
  cats.splice(idx, 1);
  renderModalEditors();
}

function addCat(type) {
  const cats = type === 'events' ? config.event_categories : config.birth_categories;
  const newId = type + '_' + Date.now();
  cats.push({ id: newId, label: 'Новая категория', keywords: [] });
  renderModalEditors();
}

async function saveSettings() {
  ['events', 'births'].forEach(type => {
    const cats = type === 'events' ? config.event_categories : config.birth_categories;
    cats.forEach((cat, i) => {
      const lbl = document.getElementById(type + '_label_' + i);
      const kw = document.getElementById(type + '_kw_' + i);
      if (lbl) cat.label = lbl.value.trim();
      if (kw) cat.keywords = kw.value.split(',').map(k=>k.trim()).filter(Boolean);
    });
  });
  await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(config) });
  renderFilters();
  closeSettings();
  if (currentData) {
    const date = document.getElementById('datePicker').value;
    if (date) loadData();
  }
}

init();
</script>
</body>
</html>
"""


def fetch_wikipedia(month_day_title):
    """Fetch Wikipedia article HTML for given title like 'May_8'"""
    url = f"https://en.wikipedia.org/wiki/{month_day_title}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 WikiDayReader/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode('utf-8'), url
    except Exception as e:
        return None, str(e)


def parse_wikipedia_day(html_content, config):
    """Parse Wikipedia day article and categorize events and births."""
    from html.parser import HTMLParser
    import html as html_module

    class WikiParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_events = False
            self.in_births = False
            self.current_section = None
            self.depth = 0
            self.events_raw = []
            self.births_raw = []
            self.current_li = []
            self.in_li = False
            self.li_depth = 0
            self.collecting = False
            self.skip_tags = {'sup', 'style', 'script'}
            self.skip_depth = 0
            self.h2_text = ''
            self.in_h2 = False
            self.wiki_title = ''

        def handle_starttag(self, tag, attrs):
            attrs_dict = dict(attrs)
            if tag in self.skip_tags:
                self.skip_depth += 1
                return
            if self.skip_depth > 0:
                return

            if tag == 'h2':
                self.in_h2 = True
                self.h2_text = ''
            elif tag == 'h1' and not self.wiki_title:
                self.in_h2 = True
                self.h2_text = '__title__'
            elif tag == 'ul' and (self.in_events or self.in_births):
                pass
            elif tag == 'li' and (self.in_events or self.in_births):
                self.in_li = True
                self.current_li = []
                self.li_content_html = ''
            elif tag == 'a' and self.in_li:
                href = attrs_dict.get('href', '')
                title = attrs_dict.get('title', '')
                if href.startswith('/wiki/'):
                    full_href = 'https://en.wikipedia.org' + href
                    self.li_content_html += f'<a href="{full_href}" target="_blank">'
                else:
                    self.li_content_html += f'<a href="{href}" target="_blank">'

        def handle_endtag(self, tag):
            if tag in self.skip_tags:
                self.skip_depth = max(0, self.skip_depth - 1)
                return
            if self.skip_depth > 0:
                return

            if tag == 'h2':
                txt = self.h2_text.strip().lower()
                self.in_events = 'event' in txt
                self.in_births = 'birth' in txt
                self.in_h2 = False
            elif tag == 'li' and self.in_li:
                entry = self.li_content_html.strip()
                if entry:
                    if self.in_events:
                        self.events_raw.append(entry)
                    elif self.in_births:
                        self.births_raw.append(entry)
                self.in_li = False
                self.li_content_html = ''
            elif tag == 'a' and self.in_li:
                self.li_content_html += '</a>'

        def handle_data(self, data):
            if self.skip_depth > 0:
                return
            if self.in_h2:
                self.h2_text += data
            elif self.in_li:
                self.li_content_html += html_module.escape(data)

    parser = WikiParser()
    parser.parse_error = None
    try:
        parser.feed(html_content)
    except Exception as e:
        parser.parse_error = str(e)

    def text_from_html(entry):
        """Extract plain text from HTML entry."""
        clean = re.sub(r'<[^>]+>', '', entry)
        return html_module.unescape(clean).lower()

    def categorize(entries, categories):
        results = []
        used = set()
        for cat in categories:
            cat_entries = []
            for i, entry in enumerate(entries):
                txt = text_from_html(entry)
                if any(re.search(r'\b' + re.escape(kw.lower()) + r'\b', txt) for kw in cat['keywords']):
                    cat_entries.append(entry)
                    used.add(i)
            results.append({'id': cat['id'], 'label': cat['label'], 'entries': cat_entries})
        return results

    def is_russian(entry):
        txt = text_from_html(entry)
        return any(re.search(r'\b' + re.escape(kw.lower()) + r'\b', txt) for kw in config.get('russian_keywords', []))

    events_categorized = categorize(parser.events_raw, config['event_categories'])
    births_categorized = categorize(parser.births_raw, config['birth_categories'])
    births_russian = [e for e in parser.births_raw if is_russian(e)]

    return {
        'events': events_categorized,
        'births': births_categorized,
        'births_russian': births_russian,
        'total_events': len(parser.events_raw),
        'total_births': len(parser.births_raw),
    }


config_store = dict(DEFAULT_CONFIG)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))

        elif path == '/api/config':
            self.send_json(config_store)

        elif path == '/api/data':
            date_str = params.get('date', [''])[0]
            if not date_str:
                self.send_json({'error': 'Не указана дата'})
                return
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                self.send_json({'error': 'Неверный формат даты'})
                return

            month_names = ['January','February','March','April','May','June',
                           'July','August','September','October','November','December']
            title = f"{month_names[d.month-1]}_{d.day}"
            wiki_url = f"https://en.wikipedia.org/wiki/{title}"

            html_content, err_or_url = fetch_wikipedia(title)
            if html_content is None:
                self.send_json({'error': f'Не удалось загрузить Википедию: {err_or_url}'})
                return

            result = parse_wikipedia_day(html_content, config_store)
            result['wiki_url'] = wiki_url
            result['wiki_title'] = title.replace('_', ' ')
            self.send_json(result)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/api/config':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                new_config = json.loads(body)
                config_store.update(new_config)
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'error': str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = 8765
    server = HTTPServer(('localhost', port), Handler)
    url = f'http://localhost:{port}'
    print(f"\n{'='*50}")
    print(f"  📖 Читатель Дней Истории")
    print(f"{'='*50}")
    print(f"  Сервер запущен: {url}")
    print(f"  Открывается браузер...")
    print(f"  Для остановки нажмите Ctrl+C")
    print(f"{'='*50}\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")


if __name__ == '__main__':
    main()
