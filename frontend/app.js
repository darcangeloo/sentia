/**
 * AI Corporate Assistant — Frontend Application
 * 
 * SPA con autenticazione JWT, chat con streaming SSE,
 * gestione documenti con drag-and-drop upload.
 */

const API_URL = 'https://sentia-i0aq.onrender.com';

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
    currentView: 'chat', // 'chat' o 'settings'
};


// ============================================================
// DOM REFERENCES
// ============================================================
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Escapa testo proveniente da dati utente/documenti prima di inserirlo via
// innerHTML (titoli conversazione, nomi file, anteprime sorgenti): senza
// questo, un filename o un titolo malevolo eseguirebbe script (stored XSS).
function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
}

// ============================================================
// ICON SYSTEM (inline SVG — nessun emoji, nessuna libreria esterna)
// ============================================================
const ICONS = {
    trash: '<path d="M4 6h12"/><path d="M8 6V4.5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1V6"/><path d="M6 6l.8 10a1 1 0 0 0 1 .9h4.4a1 1 0 0 0 1-.9L14 6"/><path d="M8.5 9v5M11.5 9v5"/>',
    edit: '<path d="M13.5 3.5a1.8 1.8 0 0 1 2.5 2.5L6 16l-4 1 1-4L13.5 3.5z"/><path d="M12 5l3 3"/>',
    send: '<polygon points="3,10 17,3.5 10.5,17 8.5,11.5 3,10"/><line x1="8.5" y1="11.5" x2="17" y2="3.5"/>',
    settings: '<circle cx="10" cy="10" r="2.6"/><path d="M10 3v2.2M10 14.8V17M3 10h2.2M14.8 10H17M5.1 5.1l1.6 1.6M13.3 13.3l1.6 1.6M14.9 5.1l-1.6 1.6M6.7 13.3l-1.6 1.6"/>',
    file: '<path d="M6.5 2.5h5l3.5 3.5v10.5a1 1 0 0 1-1 1h-7.5a1 1 0 0 1-1-1v-13a1 1 0 0 1 1-1z"/><path d="M11.5 2.5v3.5h3.5"/>',
    folder: '<path d="M3 6a1 1 0 0 1 1-1h3.5l1.5 1.8H16a1 1 0 0 1 1 1V15a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6z"/>',
    upload: '<path d="M10 13V4"/><path d="M6.5 7.5L10 4l3.5 3.5"/><path d="M4 15.5h12"/>',
    search: '<circle cx="8.5" cy="8.5" r="5.5"/><path d="M16.5 16.5L13 13"/>',
    check: '<circle cx="10" cy="10" r="7.5"/><path d="M6.5 10.3l2.3 2.3 4.7-5.2"/>',
    clock: '<circle cx="10" cy="10" r="7.5"/><path d="M10 5.5V10l3 2"/>',
    alert: '<path d="M10 3.2L17.5 16H2.5L10 3.2z"/><path d="M10 8.3v3.4"/><circle cx="10" cy="14" r="0.7" fill="currentColor" stroke="none"/>',
    chat: '<rect x="3" y="4" width="14" height="9" rx="2"/><path d="M7 13l-1.5 3 3.5-3"/>',
    sparkle: '<path d="M10 2.5l1.6 5.4L17 9.5l-5.4 1.6L10 16.5l-1.6-5.4L3 9.5l5.4-1.6L10 2.5z"/>',
    user: '<circle cx="10" cy="7" r="3"/><path d="M4 16.5c1-3.3 3.8-5 6-5s5 1.7 6 5"/>',
    'chevron-down': '<path d="M5 8l5 5 5-5"/>',
    menu: '<path d="M3 5.5h14M3 10h14M3 14.5h14"/>',
    logout: '<path d="M8 4H4.5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1H8"/><path d="M12.5 14l4-4-4-4"/><path d="M16 10H7.5"/>',
    close: '<path d="M5 5l10 10M15 5L5 15"/>',
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

const dom = {
    loginPage: $('#login-page'),
    appPage: $('#app-page'),
    loginForm: $('#login-form'),
    loginEmail: $('#login-email'),
    loginPassword: $('#login-password'),
    loginBtn: $('#login-btn'),
    loginError: $('#login-error'),
    sidebar: $('#sidebar'),
    mobileMenuBtn: $('#mobile-menu-btn'),
    mobileOverlay: $('#mobile-overlay'),
    
    // Conversations list
    chatList: $('#chat-list'),
    chatEmpty: $('#chat-empty'),
    btnNewChat: $('#btn-new-chat'),
    
    // Accordion Documents
    docsAccordionHeader: $('#docs-accordion-header'),
    docsAccordionContent: $('#docs-accordion-content'),
    docList: $('#doc-list'),
    docEmpty: $('#doc-empty'),
    uploadArea: $('#upload-area'),
    fileInput: $('#file-input'),
    uploadProgress: $('#upload-progress'),
    progressBar: $('#progress-bar'),
    uploadStatus: $('#upload-status'),
    
    // User info & actions
    userAvatar: $('#user-avatar'),
    userEmail: $('#user-email'),
    btnSettings: $('#btn-settings'),
    btnLogout: $('#btn-logout'),
    
    // Views
    chatView: $('#chat-view'),
    settingsView: $('#settings-view'),
    
    // Active chat area
    activeChatTitle: $('#active-chat-title'),
    chatMessages: $('#chat-messages'),
    chatWelcome: $('#chat-welcome'),
    chatInput: $('#chat-input'),
    btnSend: $('#btn-send'),
    btnClearChat: $('#btn-clear-chat'),
    toastContainer: $('#toast-container'),
    
    // Provider Settings inputs
    openaiApiKey: $('#openai-api-key'),
    openaiBaseUrl: $('#openai-base-url'),
    openaiModel: $('#openai-model'),
    openaiDeleteBtn: $('#openai-delete-btn'),
    openaiStatusBadge: $('#openai-status-badge'),
    
    anthropicApiKey: $('#anthropic-api-key'),
    anthropicBaseUrl: $('#anthropic-base-url'),
    anthropicModel: $('#anthropic-model'),
    anthropicDeleteBtn: $('#anthropic-delete-btn'),
    anthropicStatusBadge: $('#anthropic-status-badge'),

    geminiApiKey: $('#gemini-api-key'),
    geminiBaseUrl: $('#gemini-base-url'),
    geminiModel: $('#gemini-model'),
    geminiDeleteBtn: $('#gemini-delete-btn'),
    geminiStatusBadge: $('#gemini-status-badge'),
};


// ============================================================
// INITIALIZATION
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    injectStaticIcons();

    if (state.token) {
        showApp();
    } else {
        showLogin();
    }

    setupEventListeners();
});


function setupEventListeners() {
    // Login
    dom.loginForm.addEventListener('submit', handleLogin);
    
    // Chat views switching / clicks
    dom.btnNewChat.addEventListener('click', handleCreateConversation);
    
    // Chat inputs
    dom.chatInput.addEventListener('input', handleInputChange);
    dom.chatInput.addEventListener('keydown', handleInputKeydown);
    dom.btnSend.addEventListener('click', handleSendMessage);
    dom.btnClearChat.addEventListener('click', handleClearChat);
    
    // Event delegation for suggestion chips (since they can be dynamically populated)
    dom.chatMessages.addEventListener('click', (e) => {
        const chip = e.target.closest('.suggestion-chip');
        if (chip) {
            dom.chatInput.value = chip.dataset.query;
            handleInputChange();
            handleSendMessage();
        }
    });
    
    // Documents Accordion Toggle
    dom.docsAccordionHeader.addEventListener('click', () => {
        dom.docsAccordionHeader.classList.toggle('active');
        dom.docsAccordionContent.classList.toggle('open');
    });
    
    // Documents
    dom.uploadArea.addEventListener('click', () => dom.fileInput.click());
    dom.fileInput.addEventListener('change', handleFileSelect);
    
    // Drag and drop
    dom.uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        dom.uploadArea.classList.add('drag-over');
    });
    dom.uploadArea.addEventListener('dragleave', () => {
        dom.uploadArea.classList.remove('drag-over');
    });
    dom.uploadArea.addEventListener('drop', handleFileDrop);
    
    // Sidebar mobile
    dom.mobileMenuBtn.addEventListener('click', toggleSidebar);
    dom.mobileOverlay.addEventListener('click', closeSidebar);
    
    // Settings screen activation
    dom.btnSettings.addEventListener('click', () => {
        setView('settings');
        closeSidebar();
    });
    
    // Save provider settings
    $$('.btn-save-provider').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const provider = e.target.dataset.provider;
            saveProviderSettings(provider);
        });
    });
    
    // Delete provider settings
    $$('.btn-delete-provider').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const provider = e.target.dataset.provider;
            deleteProviderSettings(provider);
        });
    });
    // Logout
    dom.btnLogout.addEventListener('click', handleLogout);
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
    dom.loginBtn.innerHTML = '<span class="spinner"></span> Accesso in corso...';
    dom.loginError.classList.add('hidden');
    
    try {
        const formData = new URLSearchParams();
        formData.append('username', email);
        formData.append('password', password);
        
        // 1. Esegue il Login per ottenere l'access_token
        const res = await fetch(`${API_URL}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: formData,
        });
        
        if (!res.ok) {
            throw new Error('Credenziali non valide');
        }
        
        const loginData = await res.json();
        state.token = loginData.access_token;
        state.userEmail = email;
        localStorage.setItem('rag_token', state.token);
        localStorage.setItem('rag_email', email);
        
        // 2. Recupera i dettagli del profilo e dell'azienda dal nuovo endpoint
        const profileRes = await fetch(`${API_URL}/v1/users/me`, {
            method: 'GET',
            headers: { 
                'Authorization': `Bearer ${state.token}`,
                'Content-Type': 'application/json'
            }
        });

        if (!profileRes.ok) {
            throw new Error('Impossibile recuperare le informazioni del profilo aziendale');
        }
        
        const data = await profileRes.json(); 
        console.log("DATI RICEVUTI DAL SERVER:", data);

        // 3. Mostra l'applicazione (generando e iniettando l'HTML della sidebar nel DOM)
        showApp();
        
        // 4. Ricerca l'elemento in modo ultra-flessibile per evitare errori 'null'
        const sidebarTitle = document.getElementById("sidebar-title h3") 
                          || document.querySelector(".sidebar-header h3") 
                          || document.querySelector(".sidebar h3")
                          || document.querySelector(".sidebar-brand h3");

        // 5. Applica il testo solo se l'elemento è stato effettivamente individuato
        if (sidebarTitle) {
            if (data && data.company && data.company.name) {
                sidebarTitle.textContent = data.company.name;
            } else {
                // Fallback nel caso in cui l'endpoint non risponda con l'oggetto atteso
                const domainName = email.split('@')[1].split('.')[0];
                sidebarTitle.textContent = domainName.charAt(0).toUpperCase() + domainName.slice(1);
            }
        } else {
            console.warn("Elemento del titolo della sidebar non trovato nell'HTML corrente.");
        }        
        
        showToast('Accesso effettuato con successo!', 'success');
        
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
    localStorage.removeItem('rag_token');
    localStorage.removeItem('rag_email');
    showLogin();
    showToast('Disconnessione effettuata', 'info');
}


// ============================================================
// PAGE / VIEW MANAGEMENT
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
        <li class="skeleton-row">
            <span class="skeleton-block" style="width:${70 - i * 12}%"></span>
        </li>
    `).join('');
}

function showApp() {
    dom.loginPage.classList.add('hidden');
    dom.appPage.classList.remove('hidden');

    // Set user info
    if (state.userEmail) {
        dom.userEmail.textContent = state.userEmail;
        dom.userAvatar.textContent = state.userEmail.charAt(0).toUpperCase();
    }

    // Skeleton di caricamento (evita liste vuote mentre le richieste sono in volo)
    renderSkeletonRows(dom.chatList);
    renderSkeletonRows(dom.docList);

    // Load lists
    loadDocuments();
    loadConversations();
    loadLLMSettings();
    setView('chat');
}

function setView(view) {
    state.currentView = view;
    if (view === 'chat') {
        dom.chatView.classList.remove('hidden');
        dom.settingsView.classList.add('hidden');
        dom.btnSettings.style.color = 'var(--text-secondary)';
    } else if (view === 'settings') {
        dom.chatView.classList.add('hidden');
        dom.settingsView.classList.remove('hidden');
        dom.btnSettings.style.color = 'var(--accent-primary-light)';
    }
}


// ============================================================
// CONVERSATIONS (CHAT HISTORY)
// ============================================================
async function loadConversations() {
    try {
        const res = await apiFetch('/v1/conversations');
        state.conversations = await res.json();
        
        renderConversations();
        
        // Se non c'è nessuna conversazione attiva, ne creiamo una nuova
        if (state.conversations.length === 0) {
            handleCreateConversation();
        } else if (!state.activeConversationId) {
            // Seleziona la conversazione più recente
            selectConversation(state.conversations[0].id);
        }
    } catch (err) {
        console.error('Errore caricamento conversazioni:', err);
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
        
        const safeTitle = escapeHtml(conv.title);
        li.innerHTML = `
            <span class="chat-item-icon">${iconMarkup('chat', 'icon-sm')}</span>
            <span class="chat-title" title="${safeTitle}">${safeTitle}</span>
            <div class="chat-item-actions">
                <button class="chat-item-btn btn-rename" title="Rinomina" aria-label="Rinomina conversazione">${iconMarkup('edit', 'icon-sm')}</button>
                <button class="chat-item-btn btn-delete" title="Elimina" aria-label="Elimina conversazione">${iconMarkup('trash', 'icon-sm')}</button>
            </div>
        `;
        
        // Select chat
        li.addEventListener('click', (e) => {
            // Se clicca sui bottoni rename/delete, non selezionare
            if (e.target.closest('.chat-item-btn')) return;
            selectConversation(conv.id);
        });
        
        // Rename chat
        li.querySelector('.btn-rename').addEventListener('click', (e) => {
            e.stopPropagation();
            handleRenameConversation(conv.id, conv.title);
        });
        
        // Delete chat
        li.querySelector('.btn-delete').addEventListener('click', (e) => {
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
        
        // Inserisci in cima e rendi attiva
        state.conversations.unshift(newConv);
        state.activeConversationId = newConv.id;
        
        renderConversations();
        selectConversation(newConv.id);
        showToast('Nuova chat creata', 'success');
    } catch (err) {
        showToast('Errore durante la creazione della chat', 'error');
    }
}

async function selectConversation(convId) {
    state.activeConversationId = convId;
    setView('chat');
    
    // Highlight sidebar active item
    $$('.chat-item').forEach(item => {
        if (item.dataset.id === convId) {
            item.classList.add('active');
        } else {
            item.classList.remove('active');
        }
    });
    
    // Aggiorna titolo chat
    const conv = state.conversations.find(c => c.id === convId);
    if (conv) {
        dom.activeChatTitle.textContent = conv.title;
    }
    
    // Resetta cronologia messaggi UI e carica i nuovi messaggi
    dom.chatMessages.innerHTML = '';
    dom.chatMessages.appendChild(dom.chatWelcome);
    
    try {
        const res = await apiFetch(`/v1/chat/history?conversation_id=${convId}`);
        const messages = await res.json();
        
        state.messages = messages;
        
        if (messages.length > 0) {
            dom.chatWelcome.classList.add('hidden');
            messages.forEach(msg => {
                appendMessage(msg.role, msg.content, msg.sources || null, false);
            });
            scrollToBottom();
        } else {
            dom.chatWelcome.classList.remove('hidden');
        }
    } catch (err) {
        console.error('Errore caricamento messaggi della conversazione:', err);
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

    if (!cleanTitle) {
        showToast('Il titolo non può essere vuoto', 'warning');
        return;
    }

    try {
        const res = await apiFetch(`/v1/conversations/${convId}`, {
            method: 'PUT',
            body: JSON.stringify({ title: cleanTitle })
        });
        const data = await res.json();
        
        // Aggiorna lo stato locale
        state.conversations = state.conversations.map(c => {
            if (c.id === convId) {
                return { ...c, title: data.title };
            }
            return c;
        });
        
        renderConversations();
        
        // Se è la conversazione attiva, aggiorna anche l'header
        if (convId === state.activeConversationId) {
            dom.activeChatTitle.textContent = data.title;
        }
        
        showToast('Conversazione rinominata', 'success');
    } catch (err) {
        showToast('Errore durante la rinomina', 'error');
    }
}

async function handleDeleteConversation(convId) {
    const ok = await confirmModal({
        title: 'Eliminare la conversazione?',
        message: 'Questa azione elimina la conversazione e tutti i relativi messaggi in modo permanente.',
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/conversations/${convId}`, { method: 'DELETE' });
        
        // Aggiorna lo stato locale
        state.conversations = state.conversations.filter(c => c.id !== convId);
        
        renderConversations();
        
        // Se era la conversazione selezionata, passa a un'altra
        if (convId === state.activeConversationId) {
            state.activeConversationId = null;
            if (state.conversations.length > 0) {
                selectConversation(state.conversations[0].id);
            } else {
                handleCreateConversation();
            }
        }
        
        showToast('Conversazione eliminata', 'success');
    } catch (err) {
        showToast('Errore durante l\'eliminazione della conversazione', 'error');
    }
}


// ============================================================
// DOCUMENTS
// ============================================================
async function loadDocuments() {
    try {
        const res = await apiFetch('/v1/documents');
        state.documents = await res.json();
        renderDocuments();
    } catch (err) {
        console.error('Errore caricamento documenti:', err);
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
            'ready': { icon: 'check', label: 'Pronto' },
            'processing': { icon: 'clock', label: 'Elaborazione' },
            'error': { icon: 'alert', label: 'Errore' },
        }[statusClass] || { icon: 'file', label: statusClass };

        const meta = [];
        if (doc.page_count) meta.push(`${doc.page_count} pag.`);
        if (doc.chunk_count) meta.push(`${doc.chunk_count} chunks`);

        const safeFilename = escapeHtml(doc.filename);
        li.innerHTML = `
            <span class="doc-icon">${iconMarkup('file')}</span>
            <div class="doc-info">
                <div class="doc-name" title="${safeFilename}">${safeFilename}</div>
                <div class="doc-meta">
                    <span class="doc-status ${statusClass}">${iconMarkup(statusMeta.icon, 'icon-sm')} ${statusMeta.label}</span>
                    ${meta.length ? ' · ' + meta.join(' · ') : ''}
                </div>
            </div>
            <button class="doc-delete-btn" title="Elimina documento" aria-label="Elimina documento" data-id="${escapeHtml(doc.id)}">${iconMarkup('trash', 'icon-sm')}</button>
        `;

        li.querySelector('.doc-delete-btn').addEventListener('click', () => deleteDocument(doc.id));
        dom.docList.appendChild(li);
    });
}

async function deleteDocument(docId) {
    const ok = await confirmModal({
        title: 'Eliminare il documento?',
        message: 'Il file e tutti i suoi dati vettoriali (chunk ed embedding) verranno eliminati in modo permanente.',
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/documents/${docId}`, { method: 'DELETE' });
        showToast('Documento eliminato con successo', 'success');
        loadDocuments();
    } catch (err) {
        showToast('Errore durante l\'eliminazione', 'error');
    }
}

function handleFileDrop(e) {
    e.preventDefault();
    dom.uploadArea.classList.remove('drag-over');
    
    const file = e.dataTransfer.files[0];
    if (file && file.type === 'application/pdf') {
        uploadFile(file);
    } else {
        showToast('Solo file PDF sono supportati', 'error');
    }
}

function handleFileSelect(e) {
    const file = e.target.files[0];
    if (file) {
        uploadFile(file);
    }
    e.target.value = ''; // Reset per permettere re-upload dello stesso file
}

async function uploadFile(file) {
    dom.uploadProgress.classList.add('active');
    dom.progressBar.style.width = '30%';
    dom.uploadStatus.textContent = `Caricamento ${file.name}...`;
    
    try {
        const formData = new FormData();
        formData.append('file', file);
        
        dom.progressBar.style.width = '60%';
        
        const res = await apiFetch('/v1/documents/upload', {
            method: 'POST',
            body: formData,
            rawBody: true,
        });
        
        dom.progressBar.style.width = '100%';
        dom.uploadStatus.textContent = 'Indicizzazione in corso...';
        
        const data = await res.json();
        showToast(`${file.name} caricato! Indicizzazione in corso...`, 'success');
        
        if (data.document_id) {
            pollDocumentStatus(data.document_id);
        }
        
        loadDocuments();
        
    } catch (err) {
        showToast('Errore durante il caricamento', 'error');
    } finally {
        setTimeout(() => {
            dom.uploadProgress.classList.remove('active');
            dom.progressBar.style.width = '0%';
        }, 1500);
    }
}

async function pollDocumentStatus(docId) {
    const maxAttempts = 60; // 5 minuti max
    let attempts = 0;
    
    const poll = async () => {
        attempts++;
        if (attempts > maxAttempts) return;
        
        try {
            const res = await apiFetch(`/v1/documents/${docId}/status`);
            const data = await res.json();
            
            if (data.status === 'ready') {
                showToast(`Documento indicizzato: ${data.chunk_count} chunks generati`, 'success');
                loadDocuments();
                return;
            } else if (data.status === 'error') {
                showToast('Errore durante l\'indicizzazione del documento', 'error');
                loadDocuments();
                return;
            }
            
            setTimeout(poll, 5000);
        } catch (err) {
            // ignore
        }
    };
    
    setTimeout(poll, 3000);
}


// ============================================================
// CHAT
// ============================================================
function handleInputChange() {
    const input = dom.chatInput;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 150) + 'px';
    
    dom.btnSend.disabled = !input.value.trim() || state.isStreaming;
}

function handleInputKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!dom.btnSend.disabled) {
            handleSendMessage();
        }
    }
}

async function handleSendMessage() {
    const query = dom.chatInput.value.trim();
    if (!query || state.isStreaming) return;
    
    // Nascondi welcome, mostra messaggi
    dom.chatWelcome.classList.add('hidden');
    
    // Aggiungi il messaggio dell'utente
    appendMessage('user', query);
    state.messages.push({ role: 'user', content: query });
    
    // Reset input
    dom.chatInput.value = '';
    dom.chatInput.style.height = 'auto';
    dom.btnSend.disabled = true;
    
    const typingEl = showTypingIndicator();
    state.isStreaming = true;
    
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
            showToast('Sessione scaduta, effettua nuovamente il login', 'error');
            return;
        }
        
        if (!res.ok) {
            const errData = await res.json();
            throw new Error(errData.detail || 'Errore comunicazione con il server');
        }
        
        typingEl.remove();
        
        const { contentEl, messageEl } = appendMessage('assistant', '', null, true);
        let fullContent = '';
        let sources = [];
        
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // Keep incomplete line
            
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const event = JSON.parse(line.slice(6));
                        
                        if (event.type === 'sources') {
                            sources = event.data;
                        } else if (event.type === 'token') {
                            fullContent += event.data;
                            contentEl.innerHTML = formatMarkdown(fullContent);
                            scrollToBottom();
                        } else if (event.type === 'done') {
                            if (sources.length > 0) {
                                const sourcesEl = createSourcesPanel(sources);
                                messageEl.querySelector('.message-body').appendChild(sourcesEl);
                            }
                            state.messages.push({ role: 'assistant', content: fullContent, sources });
                            
                            // Aggiorna il titolo locale della conversazione se era la prima domanda
                            const actConv = state.conversations.find(c => c.id === state.activeConversationId);
                            if (actConv && (actConv.title === 'Nuova conversazione' || actConv.title === 'Nuova Chat')) {
                                loadConversations();
                            }
                        } else if (event.type === 'error') {
                            contentEl.innerHTML = `<p style="color: var(--color-error)">${escapeHtml(event.data)}</p>`;
                        }
                    } catch (parseErr) {
                        // ignore
                    }
                }
            }
        }
        
    } catch (err) {
        typingEl.remove();
        appendMessage('assistant', `Errore: ${err.message}. Riprova.`);
        console.error('Chat error:', err);
    } finally {
        state.isStreaming = false;
        handleInputChange();
        scrollToBottom();
    }
}

async function handleClearChat() {
    const ok = await confirmModal({
        title: 'Cancellare la conversazione?',
        message: 'La conversazione corrente e tutti i relativi messaggi verranno cancellati in modo permanente.',
        confirmLabel: 'Cancella',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/conversations/${state.activeConversationId}`, { method: 'DELETE' });
        
        // Rimuove da locale ed eventualmente ne crea una nuova
        state.conversations = state.conversations.filter(c => c.id !== state.activeConversationId);
        state.activeConversationId = null;
        
        showToast('Conversazione cancellata', 'info');
        loadConversations();
    } catch (err) {
        showToast('Errore durante la cancellazione', 'error');
    }
}


// ============================================================
// MESSAGE RENDERING
// ============================================================
function appendMessage(role, content, sources = null, isStreaming = false) {
    const messageEl = document.createElement('div');
    messageEl.className = `message ${role}`;
    
    const avatar = iconMarkup(role === 'user' ? 'user' : 'sparkle', 'icon-sm');
    const sender = role === 'user' ? 'Tu' : 'Sentia';

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content';
    contentEl.innerHTML = isStreaming ? '' : formatMarkdown(content);

    messageEl.innerHTML = `
        <div class="message-avatar">${avatar}</div>
        <div class="message-body">
            <div class="message-sender">${sender}</div>
        </div>
    `;
    
    messageEl.querySelector('.message-body').appendChild(contentEl);
    
    if (sources && sources.length > 0) {
        const sourcesEl = createSourcesPanel(sources);
        messageEl.querySelector('.message-body').appendChild(sourcesEl);
    }
    
    dom.chatMessages.appendChild(messageEl);
    scrollToBottom();
    
    return { contentEl, messageEl };
}

function showTypingIndicator() {
    const el = document.createElement('div');
    el.className = 'message assistant';
    el.innerHTML = `
        <div class="message-avatar">${iconMarkup('sparkle', 'icon-sm')}</div>
        <div class="message-body">
            <div class="message-sender">Assistente AI</div>
            <div class="typing-indicator">
                <span></span><span></span><span></span>
            </div>
        </div>
    `;
    dom.chatMessages.appendChild(el);
    scrollToBottom();
    return el;
}

function createSourcesPanel(sources) {
    const panel = document.createElement('div');
    panel.className = 'sources-panel';
    
    const toggle = document.createElement('button');
    toggle.className = 'sources-toggle';
    toggle.innerHTML = `${iconMarkup('search', 'icon-sm')} Fonti utilizzate (${sources.length}) <span class="chevron">${iconMarkup('chevron-down', 'icon-sm')}</span>`;
    
    const list = document.createElement('div');
    list.className = 'sources-list';
    
    sources.forEach(src => {
        const item = document.createElement('div');
        item.className = 'source-item';
        
        const pageInfo = src.page_number ? `Pagina ${src.page_number}` : '';
        const scoreHtml = src.relevance_score
            ? `<span class="source-score">${Math.round(src.relevance_score * 100)}% rilevanza</span>`
            : '';

        item.innerHTML = `
            <div class="source-filename">${iconMarkup('file', 'icon-sm')} ${escapeHtml(src.filename)} ${scoreHtml}</div>
            ${pageInfo ? `<div class="source-page">${escapeHtml(pageInfo)}</div>` : ''}
            <div class="source-preview">${escapeHtml(src.text_preview)}</div>
        `;
        list.appendChild(item);
    });
    
    toggle.addEventListener('click', () => {
        toggle.classList.toggle('expanded');
        list.classList.toggle('visible');
    });
    
    panel.appendChild(toggle);
    panel.appendChild(list);
    return panel;
}

function formatMarkdown(text) {
    if (!text) return '';
    
    let html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/`(.*?)`/g, '<code>$1</code>')
        .replace(/\n\n/g, '</p><p>')
        .replace(/\n/g, '<br>');
    
    html = '<p>' + html + '</p>';
    html = html.replace(/<p>\s*<\/p>/g, '');
    return html;
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
    });
}


// ============================================================
// SETTINGS VIEWS MANAGEMENT
// ============================================================
async function loadLLMSettings() {
    try {
        const res = await apiFetch('/v1/settings/llm');
        state.llmSettings = await res.json();
        renderLLMSettings();
    } catch (err) {
        console.error('Errore caricamento impostazioni LLM:', err);
    }
}

function renderLLMSettings() {
    // Reset cards to default inactive
    $$('.provider-card').forEach(c => {
        c.classList.remove('active');
    });
    $$('.provider-status-badge').forEach(b => {
        b.textContent = 'Inattivo';
        b.className = 'provider-status-badge inactive';
    });
    $$('.btn-delete-provider').forEach(b => {
        b.classList.add('hidden');
    });
    
    // Clear inputs values
    dom.openaiApiKey.placeholder = 'Inserisci API Key...';
    dom.anthropicApiKey.placeholder = 'Inserisci API Key...';
    dom.geminiApiKey.placeholder = 'Inserisci API Key...';
    
    // Populate based on saved settings
    state.llmSettings.forEach(s => {
        const prov = s.provider;
        const card = $(`#card-${prov}`);
        const badge = $(`#${prov}-status-badge`);
        const delBtn = $(`#${prov}-delete-btn`);
        
        if (card) {
            if (s.is_active) {
                card.classList.add('active');
                badge.textContent = 'Attivo';
                badge.className = 'provider-status-badge active';
            }
            delBtn.classList.remove('hidden');
            
            if (prov === 'openai') {
                if (s.has_api_key) dom.openaiApiKey.value = '••••••••';
                dom.openaiBaseUrl.value = s.base_url || '';
                dom.openaiModel.value = s.model || 'gpt-5.6-terra';
            } else if (prov === 'anthropic') {
                if (s.has_api_key) dom.anthropicApiKey.value = '••••••••';
                dom.anthropicBaseUrl.value = s.base_url || '';
                dom.anthropicModel.value = s.model || 'claude-opus-4.8';
            } else if (prov === 'gemini') {
                if (s.has_api_key) dom.geminiApiKey.value = '••••••••';
                dom.geminiBaseUrl.value = s.base_url || '';
                dom.geminiModel.value = s.model || 'gemini-3.1-pro';
            }
        }
    });
}

async function saveProviderSettings(provider) {
    let payload = {
        provider: provider,
        is_active: true // Salva ed attiva come attivo
    };
    
    const saveBtn = $(`.btn-save-provider[data-provider="${provider}"]`);
    const originalText = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner"></span> Validazione...';
    
    if (provider === 'openai') {
        payload.api_key = dom.openaiApiKey.value.trim();
        payload.base_url = dom.openaiBaseUrl.value.trim() || null;
        payload.model = dom.openaiModel.value;
    } else if (provider === 'anthropic') {
        payload.api_key = dom.anthropicApiKey.value.trim();
        payload.base_url = dom.anthropicBaseUrl.value.trim() || null;
        payload.model = dom.anthropicModel.value;
    } else if (provider === 'gemini') {
        payload.api_key = dom.geminiApiKey.value.trim();
        payload.base_url = dom.geminiBaseUrl.value.trim() || null;
        payload.model = dom.geminiModel.value;
    }
    
    try {
        const res = await apiFetch('/v1/settings/llm', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
        
        showToast(`Provider ${provider.toUpperCase()} salvato ed attivato!`, 'success');
        
        // Reload all settings
        await loadLLMSettings();
    } catch (err) {
        showToast(`Impossibile attivare ${provider.toUpperCase()}: ${err.message}`, 'error');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = originalText;
    }
}

async function deleteProviderSettings(provider) {
    const ok = await confirmModal({
        title: 'Eliminare la configurazione?',
        message: `La configurazione salvata per ${provider.toUpperCase()} verrà eliminata in modo permanente.`,
        confirmLabel: 'Elimina',
        danger: true,
    });
    if (!ok) return;

    try {
        await apiFetch(`/v1/settings/llm/${provider}`, { method: 'DELETE' });
        
        // Svuota i campi grafici
        if (provider === 'openai') {
            dom.openaiApiKey.value = '';
            dom.openaiBaseUrl.value = '';
        } else if (provider === 'anthropic') {
            dom.anthropicApiKey.value = '';
            dom.anthropicBaseUrl.value = '';
        } else if (provider === 'gemini') {
            dom.geminiApiKey.value = '';
            dom.geminiBaseUrl.value = '';
        }
        
        showToast(`Configurazione ${provider.toUpperCase()} eliminata`, 'info');
        await loadLLMSettings();
    } catch (err) {
        showToast('Errore durante la cancellazione delle impostazioni', 'error');
    }
}



// ============================================================
// SIDEBAR MOBILE
// ============================================================
function toggleSidebar() {
    dom.sidebar.classList.toggle('open');
    dom.mobileOverlay.classList.toggle('active');
}

function closeSidebar() {
    dom.sidebar.classList.remove('open');
    dom.mobileOverlay.classList.remove('active');
}


// ============================================================
// TOAST NOTIFICATIONS
// ============================================================
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;

    dom.toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 4000);
}


// ============================================================
// MODAL (sostituisce alert/prompt/confirm nativi del browser)
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
            </div>
        `;

        const settle = (value) => {
            closeModal(overlay);
            resolve(value);
        };

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) settle(buttons.find(b => b.isCancel)?.value ?? null);
        });

        buttons.forEach((b, i) => {
            overlay.querySelector(`[data-action="${i}"]`).addEventListener('click', () => {
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
            ? overlay.querySelector(initialFocusSelector)
            : overlay.querySelector('.modal-footer button');
        toFocus?.focus();
    });
}

function confirmModal({ title, message, confirmLabel = 'Conferma', danger = false }) {
    return openModal({
        title,
        bodyHtml: `<p>${escapeHtml(message)}</p>`,
        buttons: [
            { label: 'Annulla', className: 'btn-secondary', value: false, isCancel: true },
            { label: confirmLabel, className: danger ? 'btn-danger' : 'btn-primary', value: true, isPrimary: true },
        ],
    });
}

function promptModal({ title, label, defaultValue = '', confirmLabel = 'Salva' }) {
    return openModal({
        title,
        bodyHtml: `
            <label class="form-label" for="modal-prompt-input">${escapeHtml(label)}</label>
            <input type="text" id="modal-prompt-input" class="form-input" value="${escapeHtml(defaultValue)}">
        `,
        buttons: [
            { label: 'Annulla', className: 'btn-secondary', value: null, isCancel: true },
            {
                label: confirmLabel,
                className: 'btn-primary',
                isPrimary: true,
                value: (overlay) => overlay.querySelector('#modal-prompt-input').value.trim(),
            },
        ],
        initialFocusSelector: '#modal-prompt-input',
    });
}


// ============================================================
// API HELPER
// ============================================================
async function apiFetch(endpoint, options = {}) {
    const headers = {
        'Authorization': `Bearer ${state.token}`,
    };
    
    if (!options.rawBody && options.body && typeof options.body === 'string') {
        headers['Content-Type'] = 'application/json';
    }
    
    const res = await fetch(`${API_URL}${endpoint}`, {
        ...options,
        headers: { ...headers, ...options.headers },
    });
    
    if (res.status === 401) {
        handleLogout();
        showToast('Sessione scaduta, effettua nuovamente il login', 'error');
        throw new Error('Unauthorized');
    }
    
    if (!res.ok) {
        const errDetail = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(errDetail.detail || `HTTP ${res.status}`);
    }
    
    return res;
}
