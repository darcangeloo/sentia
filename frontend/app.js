/**
 * Sentia — Frontend
 *
 * SPA senza framework: fetch + DOM, nessuna dipendenza esterna.
 * Tre aree: sidebar (conversazioni + archivio), chat, ledger rail (fonti).
 *
 * Le due parti non ovvie sono documentate dove stanno:
 *   - UPLOAD QUEUE  : coda FIFO client-side, un file alla volta
 *   - STREAM        : stato dell'agente, fonti live, coda di rendering
 */

const API_URL = 'https://api.asksentia.com';

// ============================================================
// STATE
// ============================================================
const state = {
    token: localStorage.getItem('rag_token'),
    userEmail: localStorage.getItem('rag_email'),
    conversations: [],
    activeConversationId: null,
    messages: [],
    documents: [],
    llmSettings: [],
    isStreaming: false,
    currentView: 'chat',

    // Rail fonti
    railOpen: localStorage.getItem('rag_rail') !== 'closed',
    sourcesByKey: new Map(),   // chiave messaggio -> { sources, question }
    activeSourceKey: null,
    msgSeq: 0,

    // Coda upload
    queue: [],
    queueRunning: false,
    queueSeq: 0,
    uploadDurations: [],       // ms per file completato, per la stima

    // Il tema è già stato applicato dallo script inline in <head>; qui
    // leggiamo il valore effettivo invece di ricalcolarlo.
    theme: document.documentElement.getAttribute('data-theme') || 'dark',
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => root.querySelectorAll(sel);

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
}

// ============================================================
// ICONE (SVG inline, nessuna libreria)
// ============================================================
const ICONS = {
    trash: '<path d="M4 6h12"/><path d="M8 6V4.5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1V6"/><path d="M6 6l.8 10a1 1 0 0 0 1 .9h4.4a1 1 0 0 0 1-.9L14 6"/><path d="M8.5 9v5M11.5 9v5"/>',
    edit: '<path d="M13.5 3.5a1.8 1.8 0 0 1 2.5 2.5L6 16l-4 1 1-4L13.5 3.5z"/><path d="M12 5l3 3"/>',
    send: '<polygon points="3,10 17,3.5 10.5,17 8.5,11.5 3,10"/><line x1="8.5" y1="11.5" x2="17" y2="3.5"/>',
    settings: '<circle cx="10" cy="10" r="2.6"/><path d="M10 3v2.2M10 14.8V17M3 10h2.2M14.8 10H17M5.1 5.1l1.6 1.6M13.3 13.3l1.6 1.6M14.9 5.1l-1.6 1.6M6.7 13.3l-1.6 1.6"/>',
    file: '<path d="M6.5 2.5h5l3.5 3.5v10.5a1 1 0 0 1-1 1h-7.5a1 1 0 0 1-1-1v-13a1 1 0 0 1 1-1z"/><path d="M11.5 2.5v3.5h3.5"/>',
    folder: '<path d="M3 6a1 1 0 0 1 1-1h3.5l1.5 1.8H16a1 1 0 0 1 1 1V15a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6z"/>',
    upload: '<path d="M10 13V4"/><path d="M6.5 7.5L10 4l3.5 3.5"/><path d="M4 15.5h12"/>',
    check: '<circle cx="10" cy="10" r="7.5"/><path d="M6.5 10.3l2.3 2.3 4.7-5.2"/>',
    clock: '<circle cx="10" cy="10" r="7.5"/><path d="M10 5.5V10l3 2"/>',
    alert: '<path d="M10 3.2L17.5 16H2.5L10 3.2z"/><path d="M10 8.3v3.4"/><circle cx="10" cy="14" r="0.7" fill="currentColor" stroke="none"/>',
    chat: '<rect x="3" y="4" width="14" height="9" rx="2"/><path d="M7 13l-1.5 3 3.5-3"/>',
    sparkle: '<path d="M10 2.5l1.6 5.4L17 9.5l-5.4 1.6L10 16.5l-1.6-5.4L3 9.5l5.4-1.6L10 2.5z"/>',
    user: '<circle cx="10" cy="7" r="3"/><path d="M4 16.5c1-3.3 3.8-5 6-5s5 1.7 6 5"/>',
    menu: '<path d="M3 5.5h14M3 10h14M3 14.5h14"/>',
    logout: '<path d="M8 4H4.5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1H8"/><path d="M12.5 14l4-4-4-4"/><path d="M16 10H7.5"/>',
    close: '<path d="M5 5l10 10M15 5L5 15"/>',
    plus: '<path d="M10 4v12M4 10h12"/>',
    panel: '<rect x="2.5" y="4" width="15" height="12" rx="2"/><path d="M12.5 4v12"/>',
    download: '<path d="M10 3v9"/><path d="M6.5 8.5L10 12l3.5-3.5"/><path d="M4 15.5h12"/>',
    sun: '<circle cx="10" cy="10" r="3.4"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4"/>',
    moon: '<path d="M16 11.4A6.6 6.6 0 0 1 8.6 4a6.6 6.6 0 1 0 7.4 7.4z"/>',
};

function iconMarkup(name, extraClass = '') {
    const inner = ICONS[name];
    if (!inner) return '';
    return `<svg class="icon ${extraClass}" viewBox="0 0 20 20" aria-hidden="true">${inner}</svg>`;
}

function injectStaticIcons(root = document) {
    root.querySelectorAll('[data-icon]').forEach(el => {
        el.innerHTML = iconMarkup(el.dataset.icon);
    });
}

const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// ============================================================
// DOM
// ============================================================
const dom = {
    loginPage: $('#login-page'),
    appPage: $('#app-page'),
    loginForm: $('#login-form'),
    loginEmail: $('#login-email'),
    loginPassword: $('#login-password'),
    loginBtn: $('#login-btn'),
    loginError: $('#login-error'),

    sidebar: $('#sidebar'),
    sidebarTenant: $('#sidebar-tenant'),
    mobileOverlay: $('#mobile-overlay'),
    navTabs: $$('.nav-tab'),
    navArchiveBadge: $('#nav-archive-badge'),

    chatList: $('#chat-list'),
    chatEmpty: $('#chat-empty'),
    btnNewChat: $('#btn-new-chat'),

    docList: $('#doc-list'),
    docEmpty: $('#doc-empty'),
    uploadArea: $('#upload-area'),
    fileInput: $('#file-input'),
    queueSection: $('#queue-section'),
    queueList: $('#queue-list'),
    queueSummary: $('#queue-summary'),
    queueClear: $('#queue-clear'),

    userAvatar: $('#user-avatar'),
    userEmail: $('#user-email'),
    btnSettings: $('#btn-settings'),
    btnLogout: $('#btn-logout'),
    btnTheme: $('#btn-theme'),

    chatView: $('#chat-view'),
    settingsView: $('#settings-view'),
    btnBackChat: $('#btn-back-chat'),

    activeChatTitle: $('#active-chat-title'),
    chatMessages: $('#chat-messages'),
    chatWelcome: $('#chat-welcome'),
    chatInput: $('#chat-input'),
    composer: $('#composer'),
    btnSend: $('#btn-send'),
    btnClearChat: $('#btn-clear-chat'),
    toastContainer: $('#toast-container'),

    appContainer: $('#app-page'),
    rail: $('#sources-rail'),
    railBody: $('#rail-body'),
    railCount: $('#rail-count'),
    railClose: $('#rail-close'),
    railOpen: $('#rail-open'),
    btnToggleRail: $('#btn-toggle-rail'),

    openaiApiKey: $('#openai-api-key'),
    openaiBaseUrl: $('#openai-base-url'),
    openaiModel: $('#openai-model'),
    openaiDeleteBtn: $('#openai-delete-btn'),

    anthropicApiKey: $('#anthropic-api-key'),
    anthropicBaseUrl: $('#anthropic-base-url'),
    anthropicModel: $('#anthropic-model'),
    anthropicDeleteBtn: $('#anthropic-delete-btn'),

    geminiApiKey: $('#gemini-api-key'),
    geminiBaseUrl: $('#gemini-base-url'),
    geminiModel: $('#gemini-model'),
    geminiDeleteBtn: $('#gemini-delete-btn'),

    outlookCard: $('#card-outlook'),
    outlookStatusBadge: $('#outlook-status-badge'),
    outlookDesc: $('#outlook-desc'),
    outlookMeta: $('#outlook-meta'),
    outlookEmail: $('#outlook-email'),
    outlookLastSync: $('#outlook-last-sync'),
    outlookCount: $('#outlook-count'),
    outlookError: $('#outlook-error'),
    btnConnectOutlook: $('#btn-connect-outlook'),
    btnSyncOutlook: $('#btn-sync-outlook'),
    btnDisconnectOutlook: $('#btn-disconnect-outlook'),
};


// ============================================================
// INIT
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    injectStaticIcons();
    applyRailState();
    applyTheme();

    if (state.token) showApp();
    else showLogin();

    setupEventListeners();
});

function setupEventListeners() {
    dom.loginForm.addEventListener('submit', handleLogin);
    dom.btnNewChat.addEventListener('click', handleCreateConversation);

    dom.chatInput.addEventListener('input', handleInputChange);
    dom.chatInput.addEventListener('keydown', handleInputKeydown);
    dom.btnSend.addEventListener('click', handleSendMessage);
    dom.btnClearChat.addEventListener('click', handleClearChat);

    dom.chatMessages.addEventListener('click', (e) => {
        const chip = e.target.closest('.suggestion-chip');
        if (chip) {
            dom.chatInput.value = chip.dataset.query;
            dom.chatInput.focus();
            handleInputChange();
            // Le domande con soggetto da completare finiscono con uno spazio:
            // in quel caso lasciamo il cursore all'utente invece di inviare.
            if (!chip.dataset.query.endsWith(' ')) handleSendMessage();
            return;
        }
        const msg = e.target.closest('.message.assistant');
        if (msg && msg.dataset.key) selectSources(msg.dataset.key);
    });

    // Navigazione sidebar
    dom.navTabs.forEach(tab => tab.addEventListener('click', () => setSidebarPanel(tab.dataset.panel)));

    // Upload
    dom.uploadArea.addEventListener('click', () => dom.fileInput.click());
    dom.uploadArea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dom.fileInput.click(); }
    });
    dom.fileInput.addEventListener('change', (e) => {
        enqueueFiles(e.target.files);
        e.target.value = '';
    });
    setupDragAndDrop();
    dom.queueClear.addEventListener('click', clearFinishedQueueItems);

    // Rail
    dom.btnToggleRail.addEventListener('click', () => setRailOpen(!state.railOpen));
    dom.railClose.addEventListener('click', () => setRailOpen(false));
    dom.railOpen.addEventListener('click', () => setRailOpen(true));

    // Sidebar mobile
    $$('.mobile-menu-btn').forEach(btn => btn.addEventListener('click', toggleSidebar));
    dom.mobileOverlay.addEventListener('click', closeSidebar);

    dom.btnSettings.addEventListener('click', () => { setView('settings'); closeSidebar(); });
    dom.btnBackChat.addEventListener('click', () => setView('chat'));
    dom.btnTheme.addEventListener('click', toggleTheme);
    dom.btnLogout.addEventListener('click', handleLogout);

    $$('.btn-save-provider').forEach(btn =>
        btn.addEventListener('click', (e) => saveProviderSettings(e.currentTarget.dataset.provider)));
    $$('.btn-delete-provider').forEach(btn =>
        btn.addEventListener('click', (e) => deleteProviderSettings(e.currentTarget.dataset.provider)));

    // Integrazione Outlook
    dom.btnConnectOutlook.addEventListener('click', connectOutlook);
    dom.btnSyncOutlook.addEventListener('click', syncOutlookNow);
    dom.btnDisconnectOutlook.addEventListener('click', disconnectOutlook);
}


// ============================================================
// AUTH
// ============================================================
async function handleLogin(e) {
    e.preventDefault();
    const email = dom.loginEmail.value.trim();
    const password = dom.loginPassword.value;
    if (!email || !password) return;

    dom.loginBtn.disabled = true;
    dom.loginBtn.innerHTML = '<span class="spinner"></span> Accesso…';
    dom.loginError.classList.add('hidden');

    try {
        const formData = new URLSearchParams();
        formData.append('username', email);
        formData.append('password', password);

        const res = await fetch(`${API_URL}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: formData,
        });
        if (!res.ok) throw new Error('Email o password non corretti.');

        const loginData = await res.json();
        state.token = loginData.access_token;
        state.userEmail = email;
        localStorage.setItem('rag_token', state.token);
        localStorage.setItem('rag_email', email);

        showApp();
    } catch (err) {
        dom.loginError.textContent = err.message;
        dom.loginError.classList.remove('hidden');
    } finally {
        dom.loginBtn.disabled = false;
        dom.loginBtn.textContent = 'Accedi';
    }
}

function handleLogout() {
    state.token = null;
    state.userEmail = null;
    state.messages = [];
    state.documents = [];
    state.conversations = [];
    state.activeConversationId = null;
    state.llmSettings = [];
    state.sourcesByKey.clear();
    state.queue = [];
    localStorage.removeItem('rag_token');
    localStorage.removeItem('rag_email');
    resetLLMSettingsForm();
    renderQueue();
    renderRail();
    showLogin();
}


// ============================================================
// VIEW
// ============================================================
function showLogin() {
    dom.loginPage.classList.remove('hidden');
    dom.appPage.classList.add('hidden');
    dom.loginEmail.value = '';
    dom.loginPassword.value = '';
    dom.loginError.classList.add('hidden');
}

function renderSkeletonRows(container, count = 3) {
    container.innerHTML = Array.from({ length: count }, (_, i) => `
        <li class="skeleton-row"><span class="skeleton-block" style="width:${72 - i * 14}%"></span></li>
    `).join('');
}

function showApp() {
    dom.loginPage.classList.add('hidden');
    dom.appPage.classList.remove('hidden');

    if (state.userEmail) {
        dom.userEmail.textContent = state.userEmail;
        dom.userAvatar.textContent = state.userEmail.charAt(0).toUpperCase();
    }

    renderSkeletonRows(dom.chatList);
    renderSkeletonRows(dom.docList);

    loadDocuments();
    loadConversations();
    loadLLMSettings();
    loadOutlookStatus();
    loadCompanyName();

    // Ritorno dal flusso OAuth Outlook: il callback backend redirige qui
    // con ?outlook=connected|denied|error. Toast + apertura Impostazioni,
    // poi si pulisce la query string per non rimostrare il toast al reload.
    const params = new URLSearchParams(window.location.search);
    const outlookOutcome = params.get('outlook');
    if (outlookOutcome) {
        history.replaceState(null, '', window.location.pathname);
        if (outlookOutcome === 'connected') {
            showToast('Outlook collegato. Import delle email avviato in background.', 'success');
        } else if (outlookOutcome === 'denied') {
            showToast('Collegamento Outlook annullato.', 'info');
        } else {
            showToast('Collegamento Outlook non riuscito. Riprova.', 'error');
        }
        setView('settings');
        return;
    }

    setView('chat');
}

async function loadCompanyName() {
    const titleEl = $('.sidebar-title');
    try {
        const res = await apiFetch('/v1/users/me');
        const data = await res.json();
        if (data?.company?.name) {
            titleEl.textContent = data.company.name;
            dom.sidebarTenant.textContent = 'Documenti aziendali';
            return;
        }
    } catch (err) {
        // Silenzioso: il nome azienda è un dettaglio, non deve rompere l'avvio.
    }
    if (state.userEmail && state.userEmail.includes('@')) {
        const domainName = state.userEmail.split('@')[1].split('.')[0];
        titleEl.textContent = domainName.charAt(0).toUpperCase() + domainName.slice(1);
        dom.sidebarTenant.textContent = 'Documenti aziendali';
    }
}

function setView(view) {
    state.currentView = view;
    const isChat = view === 'chat';
    dom.chatView.classList.toggle('hidden', !isChat);
    dom.settingsView.classList.toggle('hidden', isChat);
    dom.btnSettings.setAttribute('aria-pressed', String(!isChat));
    applyRailState();
}

function setSidebarPanel(panel) {
    dom.navTabs.forEach(tab => {
        const on = tab.dataset.panel === panel;
        tab.classList.toggle('active', on);
        tab.setAttribute('aria-selected', String(on));
    });
    $('#panel-chats').classList.toggle('active', panel === 'chats');
    $('#panel-archive').classList.toggle('active', panel === 'archive');
}

function toggleSidebar() {
    dom.sidebar.classList.toggle('open');
    dom.mobileOverlay.classList.toggle('active');
}
function closeSidebar() {
    dom.sidebar.classList.remove('open');
    dom.mobileOverlay.classList.remove('active');
}


// ============================================================
// LEDGER RAIL — fonti citate
// ============================================================
function applyRailState() {
    // La rail esiste solo accanto alla chat: in Provider la colonna si
    // chiude del tutto, altrimenti resterebbe una fascia vuota a destra.
    const inChat = state.currentView === 'chat';
    const visible = state.railOpen && inChat;

    dom.appContainer.classList.toggle('rail-collapsed', !visible);
    dom.rail.classList.toggle('hidden', !inChat);
    dom.railOpen.classList.toggle('hidden', !inChat || state.railOpen);
    dom.btnToggleRail.setAttribute('aria-expanded', String(state.railOpen));
}

function setRailOpen(open) {
    state.railOpen = open;
    localStorage.setItem('rag_rail', open ? 'open' : 'closed');
    applyRailState();
}

function registerSources(key, sources, question) {
    state.sourcesByKey.set(key, { sources: sources || [], question: question || '' });
}

function selectSources(key) {
    state.activeSourceKey = key;
    $$('.message').forEach(m => m.classList.toggle('is-selected', m.dataset.key === key));
    renderRail();
}

function renderRailSkeleton() {
    dom.railCount.textContent = '…';
    dom.railBody.innerHTML = `
        <div class="skeleton-lines">
            ${[92, 74, 84].map(w => `
                <div class="source-card">
                    <span class="skeleton-block" style="width:${w}%"></span>
                    <span class="skeleton-block" style="width:40%;margin-top:8px"></span>
                </div>`).join('')}
        </div>`;
}

function renderRail() {
    const entry = state.sourcesByKey.get(state.activeSourceKey);
    const sources = entry?.sources || [];

    dom.railCount.textContent = String(sources.length);

    if (sources.length === 0) {
        dom.railBody.innerHTML = `
            <div class="rail-empty">
                <p>Le fonti compaiono qui appena Sentia le recupera, con documento e pagina.</p>
            </div>`;
        return;
    }

    // Le query esaustive recuperano documenti interi con un filtro, non con
    // un ranking: lì un punteggio non esiste. Dichiararlo è più utile che
    // lasciare uno spazio vuoto senza spiegazione.
    const ranked = sources.some(s => typeof s.relevance_score === 'number');

    const lines = [];
    if (entry.question) lines.push(`Riferite a: ${escapeHtml(truncate(entry.question, 110))}`);
    lines.push(ranked
        ? 'Punteggio della ricerca ibrida (vettoriale + full-text).'
        : 'Ricerca esaustiva: i documenti sono filtrati per intero, non ordinati per rilevanza.');
    const context = `<div class="rail-context">${lines.join('<br>')}</div>`;

    dom.railBody.innerHTML = context + sources.map((src, idx) => {
        const score = typeof src.relevance_score === 'number' ? src.relevance_score : null;
        const pct = score !== null ? Math.max(0, Math.min(100, Math.round(score * 100))) : null;

        const badge = pct !== null
            ? `<span class="source-score" title="Punteggio ricerca ibrida: ${score}">${pct}%</span>`
            : (src.chunk_count
                ? `<span class="source-score is-unranked" title="Sezioni del documento analizzate">${src.chunk_count} sez.</span>`
                : '');

        return `
            <article class="source-card" style="--i:${idx}">
                <div class="source-head">
                    <span class="source-ordinal">${String(idx + 1).padStart(2, '0')}</span>
                    <span class="source-filename" title="${escapeHtml(src.filename)}">${escapeHtml(src.filename)}</span>
                    ${badge}
                </div>
                <div class="source-relevance">
                    ${src.page_number ? `<span class="source-page">p.${escapeHtml(String(src.page_number))}</span>` : ''}
                    ${pct !== null ? `<span class="relevance-track"><span class="relevance-fill" style="width:${pct}%"></span></span>` : ''}
                </div>
                <div class="source-preview">${escapeHtml(src.text_preview || '')}</div>
            </article>`;
    }).join('');
}

function truncate(text, max) {
    return text.length > max ? text.slice(0, max - 1) + '…' : text;
}


// ============================================================
// CONVERSAZIONI
// ============================================================
async function loadConversations() {
    try {
        const res = await apiFetch('/v1/conversations');
        state.conversations = await res.json();
        renderConversations();

        if (state.conversations.length === 0) {
            handleCreateConversation();
        } else if (!state.activeConversationId) {
            selectConversation(state.conversations[0].id);
        }
    } catch (err) {
        dom.chatList.innerHTML = '';
    }
}

function renderConversations() {
    dom.chatList.innerHTML = '';
    if (state.conversations.length === 0) {
        dom.chatEmpty.classList.remove('hidden');
        return;
    }
    dom.chatEmpty.classList.add('hidden');

    state.conversations.forEach(conv => {
        const li = document.createElement('li');
        li.className = `chat-item ${conv.id === state.activeConversationId ? 'active' : ''}`;
        li.dataset.id = conv.id;
        // Una voce di lista cliccabile deve essere raggiungibile da tastiera.
        li.tabIndex = 0;
        li.setAttribute('role', 'button');

        const safeTitle = escapeHtml(conv.title);
        li.innerHTML = `
            <span class="chat-item-icon">${iconMarkup('chat', 'icon-sm')}</span>
            <span class="chat-title" title="${safeTitle}">${safeTitle}</span>
            <span class="chat-item-actions">
                <button class="chat-item-btn btn-rename" title="Rinomina" aria-label="Rinomina">${iconMarkup('edit', 'icon-sm')}</button>
                <button class="chat-item-btn btn-delete" title="Elimina" aria-label="Elimina">${iconMarkup('trash', 'icon-sm')}</button>
            </span>`;

        li.addEventListener('click', (e) => {
            if (e.target.closest('.chat-item-btn')) return;
            selectConversation(conv.id);
            closeSidebar();
        });
        li.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            if (e.target.closest('.chat-item-btn')) return;
            e.preventDefault();
            selectConversation(conv.id);
            closeSidebar();
        });
        $('.btn-rename', li).addEventListener('click', (e) => {
            e.stopPropagation();
            handleRenameConversation(conv.id, conv.title);
        });
        $('.btn-delete', li).addEventListener('click', (e) => {
            e.stopPropagation();
            handleDeleteConversation(conv.id);
        });

        dom.chatList.appendChild(li);
    });
}

async function handleCreateConversation() {
    try {
        const res = await apiFetch('/v1/conversations', { method: 'POST' });
        const newConv = await res.json();
        state.conversations.unshift(newConv);
        state.activeConversationId = newConv.id;
        renderConversations();
        selectConversation(newConv.id);
    } catch (err) {
        showToast('Non è stato possibile creare la conversazione.', 'error');
    }
}

async function selectConversation(convId) {
    state.activeConversationId = convId;
    setView('chat');

    $$('.chat-item').forEach(item => item.classList.toggle('active', item.dataset.id === convId));

    const conv = state.conversations.find(c => c.id === convId);
    if (conv) dom.activeChatTitle.textContent = conv.title;

    dom.chatMessages.innerHTML = '';
    dom.chatMessages.appendChild(dom.chatWelcome);
    state.sourcesByKey.clear();
    state.activeSourceKey = null;
    renderRail();

    try {
        const res = await apiFetch(`/v1/chat/history?conversation_id=${convId}`);
        const messages = await res.json();
        state.messages = messages;

        if (messages.length > 0) {
            dom.chatWelcome.classList.add('hidden');
            let lastQuestion = '';
            let lastKey = null;
            messages.forEach(msg => {
                if (msg.role === 'user') lastQuestion = msg.content;
                const { key } = appendMessage(msg.role, msg.content, msg.sources || null, false, lastQuestion);
                if (msg.role === 'assistant' && msg.sources?.length) lastKey = key;
            });
            if (lastKey) selectSources(lastKey);
            scrollToBottom();
        } else {
            dom.chatWelcome.classList.remove('hidden');
        }
    } catch (err) {
        // La conversazione resta visibile anche se lo storico non arriva.
    }
}

async function handleRenameConversation(convId, oldTitle) {
    const cleanTitle = await promptModal({
        title: 'Rinomina conversazione',
        label: 'Nuovo titolo',
        defaultValue: oldTitle,
        confirmLabel: 'Rinomina',
    });
    if (cleanTitle === null) return;
    if (!cleanTitle) { showToast('Il titolo non può essere vuoto.', 'warning'); return; }

    try {
        const res = await apiFetch(`/v1/conversations/${convId}`, {
            method: 'PUT',
            body: JSON.stringify({ title: cleanTitle }),
        });
        const data = await res.json();
        state.conversations = state.conversations.map(c => c.id === convId ? { ...c, title: data.title } : c);
        renderConversations();
        if (convId === state.activeConversationId) dom.activeChatTitle.textContent = data.title;
    } catch (err) {
        showToast('Rinomina non riuscita.', 'error');
    }
}

async function handleDeleteConversation(convId) {
    const ok = await confirmModal({
        title: 'Eliminare la conversazione?',
        message: 'La conversazione e tutti i suoi messaggi vengono eliminati definitivamente.',
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/conversations/${convId}`, { method: 'DELETE' });
        state.conversations = state.conversations.filter(c => c.id !== convId);
        renderConversations();

        if (convId === state.activeConversationId) {
            state.activeConversationId = null;
            if (state.conversations.length > 0) selectConversation(state.conversations[0].id);
            else handleCreateConversation();
        }
    } catch (err) {
        showToast('Eliminazione non riuscita.', 'error');
    }
}


// ============================================================
// DOCUMENTI
// ============================================================
async function loadDocuments() {
    try {
        const res = await apiFetch('/v1/documents');
        state.documents = await res.json();
        renderDocuments();
    } catch (err) {
        dom.docList.innerHTML = '';
    }
}

function renderDocuments() {
    dom.docList.innerHTML = '';
    if (state.documents.length === 0) {
        dom.docEmpty.classList.remove('hidden');
        return;
    }
    dom.docEmpty.classList.add('hidden');

    state.documents.forEach(doc => {
        const li = document.createElement('li');
        li.className = 'doc-item';

        const statusClass = doc.status || 'ready';
        const statusMeta = {
            ready: { icon: 'check', label: 'Pronto' },
            processing: { icon: 'clock', label: 'Indicizzazione' },
            error: { icon: 'alert', label: 'Errore' },
        }[statusClass] || { icon: 'file', label: statusClass };

        const meta = [];
        if (doc.page_count) meta.push(`${doc.page_count} pag`);
        if (doc.chunk_count) meta.push(`${doc.chunk_count} sez`);

        const safeFilename = escapeHtml(doc.filename);
        li.innerHTML = `
            <span class="doc-icon">${iconMarkup('file', 'icon-sm')}</span>
            <div class="doc-info">
                <div class="doc-name" title="${safeFilename}">${safeFilename}</div>
                <div class="doc-meta">
                    <span class="doc-status ${statusClass}">${iconMarkup(statusMeta.icon, 'icon-sm')} ${statusMeta.label}</span>
                    ${meta.length ? `<span>· ${meta.join(' · ')}</span>` : ''}
                </div>
            </div>
            <span class="doc-actions">
                <button class="doc-action-btn is-download" title="Scarica ${safeFilename}" aria-label="Scarica ${safeFilename}">${iconMarkup('download', 'icon-sm')}</button>
                <button class="doc-action-btn is-delete" title="Elimina ${safeFilename}" aria-label="Elimina ${safeFilename}">${iconMarkup('trash', 'icon-sm')}</button>
            </span>`;

        $('.is-download', li).addEventListener('click', (e) => downloadDocument(doc, e.currentTarget));
        $('.is-delete', li).addEventListener('click', () => deleteDocument(doc.id, doc.filename));
        dom.docList.appendChild(li);
    });
}

/**
 * Scarica il PDF originale.
 *
 * L'endpoint richiede l'header Authorization, quindi un <a href> secco non
 * basta — e il JWT in query string finirebbe nei log del server e nella
 * cronologia del browser. Si chiede quindi al backend, che risponde in due
 * modi a seconda di dove sta il file:
 *
 *   - archivio remoto  -> JSON con una URL firmata a breve scadenza; il
 *                         file viaggia da Supabase al browser senza passare
 *                         dal server dell'applicazione;
 *   - disco del server -> il PDF stesso (documenti caricati prima della
 *                         migrazione all'archivio remoto).
 */
async function downloadDocument(doc, btn) {
    btn.disabled = true;
    try {
        const res = await apiFetch(`/v1/documents/${doc.id}/download`);
        const contentType = res.headers.get('content-type') || '';

        if (contentType.includes('application/json')) {
            const { url } = await res.json();
            triggerDownload(url, doc.filename);
            return;
        }

        const blobUrl = URL.createObjectURL(await res.blob());
        triggerDownload(blobUrl, doc.filename);
        // Il revoke immediato interrompe il salvataggio in alcuni browser.
        setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
    } catch (err) {
        showToast(err.message || 'Download non riuscito.', 'error');
    } finally {
        btn.disabled = false;
    }
}

function triggerDownload(url, filename) {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

async function deleteDocument(docId, filename) {
    const ok = await confirmModal({
        title: 'Eliminare il documento?',
        message: `${filename} e tutte le sue sezioni indicizzate vengono eliminati definitivamente. Le risposte non potranno più citarlo.`,
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/documents/${docId}`, { method: 'DELETE' });
        showToast('Documento eliminato.', 'success');
        loadDocuments();
    } catch (err) {
        showToast('Eliminazione non riuscita.', 'error');
    }
}


// ============================================================
// UPLOAD QUEUE
//
// L'utente sceglie N PDF in una sola azione e li vede tutti subito in
// coda. L'elaborazione però è rigorosamente sequenziale: la pipeline di
// embedding è il collo di bottiglia, e mandarle N file insieme allunga
// il tempo di TUTTI invece di accorciare quello del primo.
//
// Un file che fallisce viene marcato e la coda prosegue: un PDF corrotto
// non deve bloccare gli altri quattro.
// ============================================================
const QUEUE_LABELS = {
    queued: 'In attesa',
    uploading: 'In elaborazione',
    indexing: 'In elaborazione',
    done: 'Completato',
    error: 'Errore',
};

function setupDragAndDrop() {
    let depth = 0;

    // Il drop è accettato ovunque nell'app: chiedere all'utente di
    // centrare un riquadro da 200px con 5 file in mano è una piccola
    // crudeltà. Trascinando, la sidebar passa da sola su Archivio.
    window.addEventListener('dragenter', (e) => {
        if (!e.dataTransfer?.types?.includes('Files')) return;
        depth++;
        if (depth === 1 && !dom.appPage.classList.contains('hidden')) {
            setSidebarPanel('archive');
            dom.uploadArea.classList.add('drag-over');
        }
    });
    window.addEventListener('dragover', (e) => {
        if (e.dataTransfer?.types?.includes('Files')) e.preventDefault();
    });
    window.addEventListener('dragleave', (e) => {
        // Stesso filtro dell'enter: un dragleave di un trascinamento non-file
        // azzererebbe il contatore mentre i PDF sono ancora sopra la pagina.
        if (!e.dataTransfer?.types?.includes('Files')) return;
        depth = Math.max(0, depth - 1);
        if (depth === 0) dom.uploadArea.classList.remove('drag-over');
    });
    window.addEventListener('drop', (e) => {
        if (!e.dataTransfer?.files?.length) return;
        e.preventDefault();
        depth = 0;
        dom.uploadArea.classList.remove('drag-over');
        if (dom.appPage.classList.contains('hidden')) return;
        enqueueFiles(e.dataTransfer.files);
    });
}

function enqueueFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    const pdfs = files.filter(f => f.type === 'application/pdf' || /\.pdf$/i.test(f.name));
    const rejected = files.length - pdfs.length;
    if (rejected > 0) {
        showToast(`${rejected} file ${rejected === 1 ? 'ignorato' : 'ignorati'}: Sentia legge solo PDF.`, 'warning');
    }
    if (pdfs.length === 0) return;

    pdfs.forEach(file => {
        state.queue.push({
            id: `q${++state.queueSeq}`,
            file,
            name: file.name,
            size: file.size,
            status: 'queued',
            progress: 0,
            detail: '',
            docId: null,
        });
    });

    setSidebarPanel('archive');
    renderQueue();
    runQueue();
}

async function runQueue() {
    if (state.queueRunning) return;
    state.queueRunning = true;

    let item;
    while ((item = state.queue.find(i => i.status === 'queued'))) {
        await processQueueItem(item);
    }

    state.queueRunning = false;
    renderQueue();
    loadDocuments();
}

async function processQueueItem(item) {
    const startedAt = performance.now();
    item.status = 'uploading';
    item.progress = 0;
    item.detail = 'Invio del file…';
    renderQueue();

    try {
        const data = await uploadWithProgress(item.file, (fraction) => {
            // L'invio occupa la prima metà della barra: l'indicizzazione,
            // che dura di più ma non espone progresso, occupa la seconda.
            item.progress = Math.round(fraction * 50);
            item.detail = `Invio ${Math.round(fraction * 100)}%`;
            updateQueueItemDom(item);
        });

        item.docId = data.document_id || null;
        item.status = 'indexing';
        item.progress = 52;
        item.detail = 'Indicizzazione…';
        renderQueue();

        if (item.docId) {
            await waitForIndexing(item);
        }

        item.status = 'done';
        item.progress = 100;
        item.detail = item.chunkCount ? `${item.chunkCount} sezioni indicizzate` : 'Pronto per le domande';
        state.uploadDurations.push(performance.now() - startedAt);
    } catch (err) {
        item.status = 'error';
        item.progress = 100;
        item.detail = err.message || 'Caricamento non riuscito.';
    }

    renderQueue();
    loadDocuments();
}

/** POST multipart con progresso reale. fetch() non espone l'upload progress. */
function uploadWithProgress(file, onProgress) {
    return new Promise((resolve, reject) => {
        const formData = new FormData();
        formData.append('file', file);

        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${API_URL}/v1/documents/upload`);
        xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);

        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) onProgress(e.loaded / e.total);
        });

        xhr.addEventListener('load', () => {
            if (xhr.status === 401) {
                handleLogout();
                reject(new Error('Sessione scaduta.'));
                return;
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                try { resolve(JSON.parse(xhr.responseText || '{}')); }
                catch { resolve({}); }
                return;
            }
            let detail = `Errore ${xhr.status}`;
            try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* corpo non JSON */ }
            reject(new Error(detail));
        });

        xhr.addEventListener('error', () => reject(new Error('Connessione interrotta.')));
        xhr.addEventListener('abort', () => reject(new Error('Caricamento annullato.')));

        xhr.send(formData);
    });
}

/** Polling dello stato di indicizzazione. Fa avanzare la barra 52 -> 96. */
async function waitForIndexing(item) {
    const maxAttempts = 90;      // ~4,5 minuti
    const intervalMs = 3000;

    for (let attempt = 0; attempt < maxAttempts; attempt++) {
        await sleep(intervalMs);

        let data;
        try {
            const res = await apiFetch(`/v1/documents/${item.docId}/status`);
            data = await res.json();
        } catch (err) {
            continue; // un colpo a vuoto non è un fallimento: si riprova
        }

        if (data.status === 'ready') {
            item.chunkCount = data.chunk_count;
            return;
        }
        if (data.status === 'error') {
            throw new Error(data.error || 'Il PDF non è stato indicizzato: potrebbe essere protetto o solo immagini.');
        }

        // Avanzamento asintotico: si avvicina a 96 senza mai arrivarci,
        // così la barra non mente dichiarando "quasi finito".
        item.progress = Math.min(96, item.progress + (96 - item.progress) * 0.25);
        item.detail = `Indicizzazione… ${formatDuration((attempt + 1) * intervalMs)}`;
        updateQueueItemDom(item);
    }
    throw new Error('Indicizzazione più lenta del previsto. Il documento potrebbe comparire tra poco.');
}

function renderQueue() {
    const active = state.queue.filter(i => i.status === 'queued' || i.status === 'uploading' || i.status === 'indexing');
    const finished = state.queue.filter(i => i.status === 'done' || i.status === 'error');

    dom.queueSection.classList.toggle('hidden', state.queue.length === 0);
    dom.queueClear.classList.toggle('hidden', finished.length === 0);

    dom.navArchiveBadge.classList.toggle('hidden', active.length === 0);
    dom.navArchiveBadge.textContent = String(active.length);

    dom.queueSummary.textContent = buildQueueSummary(active, finished);

    dom.queueList.innerHTML = '';
    state.queue.forEach(item => dom.queueList.appendChild(buildQueueItemEl(item)));
}

function buildQueueSummary(active, finished) {
    if (state.queue.length === 0) return '';
    if (active.length === 0) return `${finished.length} completati`;

    const parts = [`${finished.length}/${state.queue.length}`];
    const eta = estimateRemaining(active.length);
    if (eta) parts.push(`≈ ${eta}`);
    return parts.join(' · ');
}

/** Stima grezza ma onesta: media dei file già completati in questa sessione. */
function estimateRemaining(activeCount) {
    if (state.uploadDurations.length === 0) return '';
    const avg = state.uploadDurations.reduce((a, b) => a + b, 0) / state.uploadDurations.length;
    return formatDuration(avg * activeCount);
}

function buildQueueItemEl(item) {
    const li = document.createElement('li');
    li.className = 'queue-item';
    li.dataset.state = item.status === 'uploading' || item.status === 'indexing' ? 'processing' : item.status;
    li.dataset.id = item.id;

    li.innerHTML = `
        <div class="queue-row">
            <span class="queue-dot"></span>
            <span class="queue-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
            <span class="queue-state">${QUEUE_LABELS[item.status]}</span>
        </div>
        ${item.detail ? `<div class="queue-detail">${escapeHtml(item.detail)}</div>` : ''}
        ${item.status === 'error' ? '<button class="queue-retry">Riprova</button>' : ''}
        <span class="queue-track"><span class="queue-fill" style="width:${item.progress}%"></span></span>`;

    const retry = $('.queue-retry', li);
    if (retry) {
        retry.addEventListener('click', () => {
            item.status = 'queued';
            item.progress = 0;
            item.detail = '';
            renderQueue();
            runQueue();
        });
    }
    return li;
}

/** Aggiorna solo barra e dettaglio: ridisegnare la lista a ogni tick
 *  farebbe ripartire l'animazione di comparsa di ogni riga. */
function updateQueueItemDom(item) {
    const li = dom.queueList.querySelector(`[data-id="${item.id}"]`);
    if (!li) return;
    const fill = $('.queue-fill', li);
    if (fill) fill.style.width = `${item.progress}%`;
    const detail = $('.queue-detail', li);
    if (detail) detail.textContent = item.detail;
}

function clearFinishedQueueItems() {
    state.queue = state.queue.filter(i => i.status !== 'done' && i.status !== 'error');
    renderQueue();
}

function formatDuration(ms) {
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));


// ============================================================
// TEMA
//
// Il bottone dice dove porta, non dove sei: chi è al buio legge "Passa
// al tema chiaro" e vede il sole. Etichetta e icona indicano entrambe
// la destinazione.
// ============================================================
function applyTheme() {
    const isDark = state.theme === 'dark';
    document.documentElement.setAttribute('data-theme', state.theme);
    dom.btnTheme.innerHTML = iconMarkup(isDark ? 'sun' : 'moon');
    dom.btnTheme.title = isDark ? 'Passa al tema chiaro' : 'Passa al tema scuro';
}

function toggleTheme() {
    state.theme = state.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('rag_theme', state.theme);
    applyTheme();
}


// ============================================================
// CHAT
// ============================================================
function handleInputChange() {
    const input = dom.chatInput;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
    dom.btnSend.disabled = !input.value.trim() || state.isStreaming;
}

function handleInputKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!dom.btnSend.disabled) handleSendMessage();
    }
}

// Micro-messaggi mostrati mentre l'agente lavora. Descrivono le fasi reali
// della pipeline (retrieval, confronto fonti, composizione); un evento
// `status` dal backend, quando arriva, li sostituisce.
const AGENT_STEPS = [
    'Cerco nei documenti…',
    'Confronto le fonti…',
    'Verifico i riferimenti…',
    'Compongo la risposta…',
];

function createAgentStatus() {
    const wrap = document.createElement('div');
    wrap.className = 'agent-status';
    wrap.innerHTML = `
        <div class="agent-status-line">
            <span class="agent-status-text">${AGENT_STEPS[0]}</span>
            <span class="caret"></span>
        </div>
        <div class="touched-sources"></div>`;

    const textEl = $('.agent-status-text', wrap);
    let idx = 0;
    let pinned = false;

    const timer = setInterval(() => {
        if (pinned) return;
        idx = (idx + 1) % AGENT_STEPS.length;
        swapText(AGENT_STEPS[idx]);
    }, 2600);

    function swapText(next) {
        textEl.classList.add('swap');
        setTimeout(() => {
            textEl.textContent = next;
            textEl.classList.remove('swap');
        }, 180);
    }

    return {
        el: wrap,
        /** Uno status reale dal backend batte sempre i messaggi generici. */
        setStatus(text) { pinned = true; swapText(text); },
        addSource(filename) {
            const badges = $('.touched-sources', wrap);
            const badge = document.createElement('span');
            badge.className = 'touched-badge';
            badge.innerHTML = `${iconMarkup('file', 'icon-sm')} ${escapeHtml(filename)}`;
            badges.appendChild(badge);
        },
        stop() { clearInterval(timer); },
    };
}

async function handleSendMessage() {
    const query = dom.chatInput.value.trim();
    if (!query || state.isStreaming) return;

    dom.chatWelcome.classList.add('hidden');
    appendMessage('user', query);
    state.messages.push({ role: 'user', content: query });

    dom.chatInput.value = '';
    dom.chatInput.style.height = 'auto';
    dom.btnSend.disabled = true;
    state.isStreaming = true;
    dom.composer.classList.add('is-generating');

    // Il messaggio dell'assistente nasce subito e ospita lo stato: così
    // la risposta non "salta" in un nuovo blocco quando inizia ad arrivare.
    const { contentEl, messageEl, key } = appendMessage('assistant', '', null, true, query);
    const status = createAgentStatus();
    contentEl.appendChild(status.el);

    renderRailSkeleton();

    let fullContent = '';
    let sources = [];

    try {
        const res = await fetch(`${API_URL}/v1/chat/stream`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${state.token}`,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ query, conversation_id: state.activeConversationId }),
        });

        if (res.status === 401) {
            handleLogout();
            showToast('Sessione scaduta. Accedi di nuovo.', 'error');
            return;
        }
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || 'Il server non ha risposto.');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let started = false;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let event;
                try { event = JSON.parse(line.slice(6)); } catch { continue; }

                if (event.type === 'sources') {
                    sources = event.data || [];
                    registerSources(key, sources, query);
                    // I badge atterrano uno alla volta: si vede il retrieval
                    // accadere, invece di un blocco che appare tutto insieme.
                    revealSourcesProgressively(sources, status, key);

                } else if (event.type === 'status') {
                    status.setStatus(event.data);

                } else if (event.type === 'token') {
                    if (!started) {
                        started = true;
                        status.stop();
                        contentEl.innerHTML = '';
                    }
                    fullContent += event.data;
                    renderStreamingContent(contentEl, fullContent);
                    scrollToBottom();

                } else if (event.type === 'done') {
                    status.stop();
                    contentEl.innerHTML = formatMarkdown(fullContent);
                    attachSourcesButton(messageEl, key, sources.length);
                    state.messages.push({ role: 'assistant', content: fullContent, sources });
                    selectSources(key);

                    const conv = state.conversations.find(c => c.id === state.activeConversationId);
                    if (conv && /^Nuova (conversazione|Chat)$/i.test(conv.title)) loadConversations();

                } else if (event.type === 'error') {
                    status.stop();
                    contentEl.innerHTML = `<p class="stream-error">${escapeHtml(event.data)}</p>`;
                }
            }
        }
    } catch (err) {
        status.stop();
        contentEl.innerHTML = `<p class="stream-error">${escapeHtml(err.message)} Riprova.</p>`;
    } finally {
        status.stop();
        state.isStreaming = false;
        dom.composer.classList.remove('is-generating');
        handleInputChange();
        renderRail();
        scrollToBottom();
    }
}

function revealSourcesProgressively(sources, status, key) {
    // La rail si disegna una volta sola: lo scaglionamento delle card è
    // nel CSS (animation-delay su --i). Ridisegnarla a ogni fonte
    // farebbe ripartire da capo l'animazione di quelle già comparse.
    state.activeSourceKey = key;
    renderRail();

    if (prefersReducedMotion) {
        sources.forEach(s => status.addSource(s.filename));
        return;
    }
    sources.forEach((src, i) => {
        setTimeout(() => status.addSource(src.filename), i * 90);
    });
}

/**
 * Rendering durante lo streaming.
 *
 * Il markdown viene ricalcolato solo sulle righe già complete; la riga in
 * corso resta testo semplice, con gli ultimi caratteri in verde. Effetto:
 * un bordo d'onda che segue la scrittura e si spegne da solo, senza
 * riformattare l'intero messaggio a ogni token.
 */
const FRESH_TAIL_CHARS = 14;

function renderStreamingContent(contentEl, full) {
    const cut = full.lastIndexOf('\n');
    const stable = cut === -1 ? '' : full.slice(0, cut);
    const tail = cut === -1 ? full : full.slice(cut + 1);

    const settled = tail.slice(0, Math.max(0, tail.length - FRESH_TAIL_CHARS));
    const fresh = tail.slice(Math.max(0, tail.length - FRESH_TAIL_CHARS));

    contentEl.innerHTML =
        formatMarkdown(stable) +
        `<p class="stream-tail">${escapeHtml(settled)}<span class="tok-fresh">${escapeHtml(fresh)}</span><span class="caret"></span></p>`;
}

function attachSourcesButton(messageEl, key, count) {
    if (!count) return;
    const foot = document.createElement('footer');
    foot.className = 'message-foot';
    foot.innerHTML = `<button class="message-sources-btn">${iconMarkup('file', 'icon-sm')} ${count} ${count === 1 ? 'fonte' : 'fonti'}</button>`;
    foot.querySelector('button').addEventListener('click', () => {
        setRailOpen(true);
        selectSources(key);
    });
    $('.message-body', messageEl).appendChild(foot);
}

async function handleClearChat() {
    const ok = await confirmModal({
        title: 'Eliminare la conversazione?',
        message: 'La conversazione corrente e tutti i suoi messaggi vengono eliminati definitivamente.',
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/conversations/${state.activeConversationId}`, { method: 'DELETE' });
        state.conversations = state.conversations.filter(c => c.id !== state.activeConversationId);
        state.activeConversationId = null;
        loadConversations();
    } catch (err) {
        showToast('Eliminazione non riuscita.', 'error');
    }
}


// ============================================================
// RENDERING MESSAGGI
// ============================================================
function appendMessage(role, content, sources = null, isStreaming = false, question = '') {
    const key = `m${++state.msgSeq}`;

    const messageEl = document.createElement('article');
    messageEl.className = `message ${role}`;
    messageEl.dataset.key = key;

    const avatar = iconMarkup(role === 'user' ? 'user' : 'sparkle', 'icon-sm');
    const sender = role === 'user' ? 'Tu' : 'Sentia';

    messageEl.innerHTML = `
        <div class="message-avatar">${avatar}</div>
        <div class="message-body">
            <header class="message-head"><span class="message-sender">${sender}</span></header>
        </div>`;

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content';
    if (!isStreaming) contentEl.innerHTML = formatMarkdown(content);
    $('.message-body', messageEl).appendChild(contentEl);

    if (sources && sources.length > 0) {
        registerSources(key, sources, question);
        attachSourcesButton(messageEl, key, sources.length);
    }

    dom.chatMessages.appendChild(messageEl);
    scrollToBottom();
    return { contentEl, messageEl, key };
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
    });
}


// ============================================================
// MARKDOWN
//
// Renderer minimo ma con le tabelle: l'output di punta del prodotto è
// l'elenco dei movimenti, e senza supporto tabellare arriverebbe a video
// come una colonna di pipe.
// ============================================================
const AMOUNT_RE = /^[-+]?\s*(€|EUR)?\s*[\d.]+(,\d{1,2})?\s*(€|EUR)?$/i;
const DATE_RE = /^\d{1,2}[\/.-]\d{1,2}[\/.-]\d{2,4}$/;

function inlineMarkdown(text) {
    return escapeHtml(text)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
}

function splitTableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
}

function cellClass(value) {
    if (AMOUNT_RE.test(value)) return ' class="num"';
    if (DATE_RE.test(value)) return ' class="mono"';
    return '';
}

/** Classe della colonna c: vale se la maggioranza delle celle concorda. */
function columnClass(rows, c) {
    if (rows.length === 0) return '';
    let amounts = 0, dates = 0;
    rows.forEach(cells => {
        const v = cells[c] ?? '';
        if (AMOUNT_RE.test(v)) amounts++;
        else if (DATE_RE.test(v)) dates++;
    });
    if (amounts > rows.length / 2) return ' class="num"';
    if (dates > rows.length / 2) return ' class="mono"';
    return '';
}

function formatMarkdown(text) {
    if (!text) return '';
    const lines = text.split('\n');
    const out = [];
    let i = 0;

    const isTableSep = (l) => /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(l) && l.includes('-');

    while (i < lines.length) {
        const line = lines[i];

        if (/^\s*$/.test(line)) { i++; continue; }

        // Tabella
        if (/^\s*\|/.test(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
            const headers = splitTableRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && /^\s*\|/.test(lines[i])) {
                rows.push(splitTableRow(lines[i]));
                i++;
            }
            // L'allineamento si decide per colonna guardando i dati, non la
            // singola cella: altrimenti l'intestazione "Importo" resta a
            // sinistra mentre i suoi numeri vanno a destra.
            const colClass = headers.map((_, c) => columnClass(rows, c));
            const thead = headers.map((h, c) =>
                `<th${colClass[c]}>${inlineMarkdown(h)}</th>`).join('');
            const tbody = rows.map(cells =>
                `<tr>${cells.map((cell, c) => `<td${colClass[c] || cellClass(cell)}>${inlineMarkdown(cell)}</td>`).join('')}</tr>`).join('');
            out.push(`<div class="md-table-wrap"><table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table></div>`);
            continue;
        }

        // Riga orizzontale
        if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push('<hr>'); i++; continue; }

        // Titolo
        const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
        if (heading) {
            const level = Math.min(heading[1].length + 2, 6);
            out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
            i++;
            continue;
        }

        // Citazione
        if (/^\s*>\s?/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
                buf.push(lines[i].replace(/^\s*>\s?/, ''));
                i++;
            }
            out.push(`<blockquote>${buf.map(inlineMarkdown).join('<br>')}</blockquote>`);
            continue;
        }

        // Elenco puntato
        if (/^\s*[-*+]\s+/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
                buf.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
                i++;
            }
            out.push(`<ul>${buf.map(b => `<li>${inlineMarkdown(b)}</li>`).join('')}</ul>`);
            continue;
        }

        // Elenco numerato
        if (/^\s*\d+[.)]\s+/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
                buf.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''));
                i++;
            }
            out.push(`<ol>${buf.map(b => `<li>${inlineMarkdown(b)}</li>`).join('')}</ol>`);
            continue;
        }

        // Paragrafo
        const buf = [];
        while (i < lines.length &&
               !/^\s*$/.test(lines[i]) &&
               !/^\s*\|/.test(lines[i]) &&
               !/^\s*(#{1,6})\s/.test(lines[i]) &&
               !/^\s*>\s?/.test(lines[i]) &&
               !/^\s*[-*+]\s+/.test(lines[i]) &&
               !/^\s*\d+[.)]\s+/.test(lines[i]) &&
               !/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i])) {
            buf.push(lines[i]);
            i++;
        }
        if (buf.length) out.push(`<p>${buf.map(inlineMarkdown).join('<br>')}</p>`);
    }

    return out.join('');
}


// ============================================================
// PROVIDER
// ============================================================
async function loadLLMSettings() {
    try {
        const res = await apiFetch('/v1/settings/llm');
        state.llmSettings = await res.json();
        renderLLMSettings();
    } catch (err) {
        // Le impostazioni non caricate non impediscono di usare la chat.
    }
}

function resetLLMSettingsForm() {
    $$('.provider-card').forEach(c => c.classList.remove('active'));
    $$('.provider-status-badge').forEach(b => {
        b.textContent = 'Inattivo';
        b.className = 'provider-status-badge inactive';
    });
    $$('.btn-delete-provider').forEach(b => b.classList.add('hidden'));

    // Svuotare davvero i campi: una chiave lasciata nel DOM resterebbe
    // visibile al prossimo utente che accede dallo stesso browser.
    [dom.openaiApiKey, dom.anthropicApiKey, dom.geminiApiKey].forEach(input => { input.value = ''; });
    [dom.openaiBaseUrl, dom.anthropicBaseUrl, dom.geminiBaseUrl].forEach(input => { input.value = ''; });
    [dom.openaiModel, dom.anthropicModel, dom.geminiModel].forEach(select => { select.selectedIndex = 0; });
}

function renderLLMSettings() {
    resetLLMSettingsForm();

    state.llmSettings.forEach(s => {
        const prov = s.provider;
        const card = $(`#card-${prov}`);
        const badge = $(`#${prov}-status-badge`);
        const delBtn = $(`#${prov}-delete-btn`);
        if (!card) return;

        if (s.is_active) {
            card.classList.add('active');
            badge.textContent = 'Attivo';
            badge.className = 'provider-status-badge active';
        }
        delBtn.classList.remove('hidden');

        const fields = {
            openai: [dom.openaiApiKey, dom.openaiBaseUrl, dom.openaiModel],
            anthropic: [dom.anthropicApiKey, dom.anthropicBaseUrl, dom.anthropicModel],
            gemini: [dom.geminiApiKey, dom.geminiBaseUrl, dom.geminiModel],
        }[prov];
        if (!fields) return;

        const [keyEl, urlEl, modelEl] = fields;
        if (s.has_api_key) keyEl.value = '••••••••••••';
        urlEl.value = s.base_url || '';
        if (s.model) modelEl.value = s.model;
    });
}

async function saveProviderSettings(provider) {
    const fields = {
        openai: [dom.openaiApiKey, dom.openaiBaseUrl, dom.openaiModel],
        anthropic: [dom.anthropicApiKey, dom.anthropicBaseUrl, dom.anthropicModel],
        gemini: [dom.geminiApiKey, dom.geminiBaseUrl, dom.geminiModel],
    }[provider];
    if (!fields) return;

    const [keyEl, urlEl, modelEl] = fields;
    const payload = {
        provider,
        is_active: true,
        api_key: keyEl.value.trim(),
        base_url: urlEl.value.trim() || null,
        model: modelEl.value,
    };

    const saveBtn = $(`.btn-save-provider[data-provider="${provider}"]`);
    const originalText = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner"></span> Verifica…';

    try {
        await apiFetch('/v1/settings/llm', { method: 'POST', body: JSON.stringify(payload) });
        showToast(`${provider.toUpperCase()} attivato.`, 'success');
        await loadLLMSettings();
    } catch (err) {
        showToast(`${provider.toUpperCase()} non attivato: ${err.message}`, 'error');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = originalText;
    }
}

async function deleteProviderSettings(provider) {
    const ok = await confirmModal({
        title: 'Eliminare la configurazione?',
        message: `La chiave salvata per ${provider.toUpperCase()} viene eliminata. Le risposte passeranno a un altro provider attivo.`,
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/settings/llm/${provider}`, { method: 'DELETE' });
        showToast(`Configurazione ${provider.toUpperCase()} eliminata.`, 'info');
        await loadLLMSettings();
    } catch (err) {
        showToast('Eliminazione non riuscita.', 'error');
    }
}


// ============================================================
// INTEGRAZIONE OUTLOOK
// ============================================================
let outlookPollTimer = null;

async function loadOutlookStatus() {
    try {
        const res = await apiFetch('/v1/integrations/outlook/status');
        renderOutlookStatus(await res.json());
    } catch (err) {
        // Stato non caricato: la card resta su "Non connesso", la chat funziona.
    }
}

function renderOutlookStatus(status) {
    const badge = dom.outlookStatusBadge;

    if (!status.connected) {
        const disconnected = status.status === 'disconnected';
        badge.textContent = disconnected ? 'Disconnesso' : 'Non connesso';
        badge.className = 'provider-status-badge inactive';
        dom.outlookCard.classList.remove('active');
        dom.outlookDesc.classList.remove('hidden');
        dom.outlookMeta.classList.add('hidden');
        dom.btnConnectOutlook.textContent = disconnected ? 'Ricollega Outlook' : 'Collega Outlook';
        dom.btnConnectOutlook.classList.remove('hidden');
        dom.btnSyncOutlook.classList.add('hidden');
        dom.btnDisconnectOutlook.classList.toggle('hidden', !disconnected);
        // Un account disconnesso (token revocato/scaduto) mostra il perché.
        const showError = disconnected && status.error_message;
        dom.outlookError.classList.toggle('hidden', !showError);
        if (showError) dom.outlookError.textContent = status.error_message;
        stopOutlookPolling();
        return;
    }

    const syncing = status.status === 'syncing';
    const hasError = status.status === 'error';
    badge.textContent = syncing ? 'Sincronizzazione…' : (hasError ? 'Errore sync' : 'Connesso');
    badge.className = `provider-status-badge ${hasError ? 'inactive' : 'active'}`;
    dom.outlookCard.classList.add('active');
    dom.outlookDesc.classList.add('hidden');

    dom.outlookMeta.classList.remove('hidden');
    dom.outlookEmail.textContent = status.email_address || '—';
    dom.outlookLastSync.textContent = status.last_sync_at
        ? new Date(status.last_sync_at).toLocaleString('it-IT')
        : (syncing ? 'in corso…' : 'mai');
    dom.outlookCount.textContent = String(status.email_count ?? 0);

    dom.outlookError.classList.toggle('hidden', !hasError);
    if (hasError) dom.outlookError.textContent = status.error_message || 'Ultima sincronizzazione fallita.';

    dom.btnConnectOutlook.classList.add('hidden');
    dom.btnSyncOutlook.classList.remove('hidden');
    dom.btnSyncOutlook.disabled = syncing;
    dom.btnDisconnectOutlook.classList.remove('hidden');

    // Durante l'import lo stato cambia da solo: si continua a leggere finché
    // il sync non finisce, per aggiornare contatore e badge senza reload.
    if (syncing) startOutlookPolling();
    else stopOutlookPolling();
}

function startOutlookPolling() {
    if (outlookPollTimer) return;
    outlookPollTimer = setInterval(loadOutlookStatus, 10000);
}

function stopOutlookPolling() {
    if (!outlookPollTimer) return;
    clearInterval(outlookPollTimer);
    outlookPollTimer = null;
}

async function connectOutlook() {
    dom.btnConnectOutlook.disabled = true;
    try {
        const res = await apiFetch('/v1/integrations/outlook/authorize');
        const data = await res.json();
        // Redirect al consenso Microsoft: si torna sul callback backend,
        // che a sua volta riporta qui con ?outlook=<esito>.
        window.location.href = data.url;
    } catch (err) {
        showToast(`Collegamento non avviato: ${err.message}`, 'error');
        dom.btnConnectOutlook.disabled = false;
    }
}

async function syncOutlookNow() {
    dom.btnSyncOutlook.disabled = true;
    try {
        await apiFetch('/v1/integrations/outlook/sync', { method: 'POST' });
        showToast('Sincronizzazione avviata.', 'info');
        await loadOutlookStatus();
    } catch (err) {
        showToast(`Sync non avviato: ${err.message}`, 'error');
        dom.btnSyncOutlook.disabled = false;
    }
}

async function disconnectOutlook() {
    const ok = await confirmModal({
        title: 'Disconnettere Outlook?',
        message: 'I token di accesso salvati vengono eliminati e il sync si ferma. Le email già indicizzate restano consultabili in chat.',
        confirmLabel: 'Disconnetti',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch('/v1/integrations/outlook', { method: 'DELETE' });
        showToast('Outlook disconnesso.', 'info');
        await loadOutlookStatus();
    } catch (err) {
        showToast('Disconnessione non riuscita.', 'error');
    }
}


// ============================================================
// TOAST
// ============================================================
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    dom.toastContainer.appendChild(toast);
    setTimeout(() => toast.remove(), 4200);
}


// ============================================================
// MODALI
// ============================================================
const modalRoot = $('#modal-root');

function closeModal(overlay) {
    overlay.remove();
    document.removeEventListener('keydown', overlay._onKeydown);
}

function openModal({ title, bodyHtml, buttons, initialFocusSelector }) {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';

        const buttonsHtml = buttons.map((b, i) =>
            `<button type="button" class="${b.className}" data-action="${i}">${escapeHtml(b.label)}</button>`
        ).join('');

        overlay.innerHTML = `
            <div class="modal-card" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
                <div class="modal-header"><h3>${escapeHtml(title)}</h3></div>
                <div class="modal-body">${bodyHtml}</div>
                <div class="modal-footer">${buttonsHtml}</div>
            </div>`;

        const settle = (value) => { closeModal(overlay); resolve(value); };

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) settle(buttons.find(b => b.isCancel)?.value ?? null);
        });

        buttons.forEach((b, i) => {
            $(`[data-action="${i}"]`, overlay).addEventListener('click', () => {
                settle(typeof b.value === 'function' ? b.value(overlay) : b.value);
            });
        });

        const onKeydown = (e) => {
            if (e.key === 'Escape') settle(buttons.find(b => b.isCancel)?.value ?? null);
            if (e.key === 'Enter' && document.activeElement?.tagName !== 'BUTTON') {
                const primary = buttons.find(b => b.isPrimary);
                if (primary) settle(typeof primary.value === 'function' ? primary.value(overlay) : primary.value);
            }
        };
        overlay._onKeydown = onKeydown;
        document.addEventListener('keydown', onKeydown);

        modalRoot.appendChild(overlay);
        const toFocus = initialFocusSelector
            ? $(initialFocusSelector, overlay)
            : $('.modal-footer button', overlay);
        toFocus?.focus();
    });
}

function confirmModal({ title, message, confirmLabel = 'Conferma', danger = false }) {
    return openModal({
        title,
        bodyHtml: `<p>${escapeHtml(message)}</p>`,
        buttons: [
            { label: 'Annulla', className: 'btn-ghost', value: false, isCancel: true },
            { label: confirmLabel, className: danger ? 'btn-danger' : 'btn-primary', value: true, isPrimary: true },
        ],
    });
}

function promptModal({ title, label, defaultValue = '', confirmLabel = 'Salva' }) {
    return openModal({
        title,
        bodyHtml: `
            <label class="form-label" for="modal-prompt-input">${escapeHtml(label)}</label>
            <input type="text" id="modal-prompt-input" class="form-input" value="${escapeHtml(defaultValue)}">`,
        buttons: [
            { label: 'Annulla', className: 'btn-ghost', value: null, isCancel: true },
            {
                label: confirmLabel,
                className: 'btn-primary',
                isPrimary: true,
                value: (overlay) => $('#modal-prompt-input', overlay).value.trim(),
            },
        ],
        initialFocusSelector: '#modal-prompt-input',
    });
}


// ============================================================
// API
// ============================================================
async function apiFetch(endpoint, options = {}) {
    const headers = { 'Authorization': `Bearer ${state.token}` };
    if (options.body && typeof options.body === 'string') {
        headers['Content-Type'] = 'application/json';
    }

    const res = await fetch(`${API_URL}${endpoint}`, {
        ...options,
        headers: { ...headers, ...options.headers },
    });

    if (res.status === 401) {
        handleLogout();
        showToast('Sessione scaduta. Accedi di nuovo.', 'error');
        throw new Error('Unauthorized');
    }
    if (!res.ok) {
        const errDetail = await res.json().catch(() => ({ detail: `Errore ${res.status}` }));
        throw new Error(errDetail.detail || `Errore ${res.status}`);
    }
    return res;
}
