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

    ollamaUrl: $('#ollama-url'),
    ollamaModel: $('#ollama-model'),
    ollamaDeleteBtn: $('#ollama-delete-btn'),
    ollamaStatusBadge: $('#ollama-status-badge'),
    btnRefreshOllama: $('#btn-refresh-ollama'),
};


// ============================================================
// INITIALIZATION
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
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
    
    // Refresh local Ollama models
    dom.btnRefreshOllama.addEventListener('click', () => {
        const url = dom.ollamaUrl.value.trim();
        loadOllamaModels(url);
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

function showApp() {
    dom.loginPage.classList.add('hidden');
    dom.appPage.classList.remove('hidden');
    
    // Set user info
    if (state.userEmail) {
        dom.userEmail.textContent = state.userEmail;
        dom.userAvatar.textContent = state.userEmail.charAt(0).toUpperCase();
    }
    
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
        
        // When opening settings, trigger models reload for ollama
        loadOllamaModels(dom.ollamaUrl.value);
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
        
        li.innerHTML = `
            <span class="chat-item-icon">💬</span>
            <span class="chat-title" title="${conv.title}">${conv.title}</span>
            <div class="chat-item-actions">
                <button class="chat-item-btn btn-rename" title="Rinomina">✏️</button>
                <button class="chat-item-btn btn-delete" title="Elimina">🗑️</button>
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
    const newTitle = prompt('Inserisci il nuovo titolo per la chat:', oldTitle);
    if (newTitle === null) return;
    
    const cleanTitle = newTitle.trim();
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
    if (!confirm('Sei sicuro di voler eliminare questa conversazione e tutti i relativi messaggi?')) return;
    
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
        const statusLabel = {
            'ready': '✅ Pronto',
            'processing': '⏳ Elaborazione',
            'error': '❌ Errore'
        }[statusClass] || statusClass;
        
        const meta = [];
        if (doc.page_count) meta.push(`${doc.page_count} pag.`);
        if (doc.chunk_count) meta.push(`${doc.chunk_count} chunks`);
        
        li.innerHTML = `
            <span class="doc-icon">📄</span>
            <div class="doc-info">
                <div class="doc-name" title="${doc.filename}">${doc.filename}</div>
                <div class="doc-meta">
                    <span class="doc-status ${statusClass}">${statusLabel}</span>
                    ${meta.length ? ' · ' + meta.join(' · ') : ''}
                </div>
            </div>
            <button class="doc-delete-btn" title="Elimina documento" data-id="${doc.id}">🗑️</button>
        `;
        
        li.querySelector('.doc-delete-btn').addEventListener('click', () => deleteDocument(doc.id));
        dom.docList.appendChild(li);
    });
}

async function deleteDocument(docId) {
    if (!confirm('Sei sicuro di voler eliminare questo documento e tutti i suoi dati vettoriali?')) return;
    
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
        showToast(`📄 ${file.name} caricato! Indicizzazione in corso...`, 'success');
        
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
                showToast(`✅ Documento indicizzato: ${data.chunk_count} chunks generati`, 'success');
                loadDocuments();
                return;
            } else if (data.status === 'error') {
                showToast('❌ Errore durante l\'indicizzazione del documento', 'error');
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
                            contentEl.innerHTML = `<p style="color: var(--color-error)">⚠️ ${event.data}</p>`;
                        }
                    } catch (parseErr) {
                        // ignore
                    }
                }
            }
        }
        
    } catch (err) {
        typingEl.remove();
        appendMessage('assistant', `⚠️ Errore: ${err.message}. Riprova.`);
        console.error('Chat error:', err);
    } finally {
        state.isStreaming = false;
        handleInputChange();
        scrollToBottom();
    }
}

async function handleClearChat() {
    if (!confirm('Vuoi cancellare questa conversazione e tutti i relativi messaggi?')) return;
    
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
    
    const avatar = role === 'user' ? '👤' : '🧠';
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
        <div class="message-avatar">🧠</div>
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
    toggle.innerHTML = `🔍 Fonti utilizzate (${sources.length}) <span class="chevron">▼</span>`;
    
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
            <div class="source-filename">📄 ${src.filename} ${scoreHtml}</div>
            ${pageInfo ? `<div class="source-page">${pageInfo}</div>` : ''}
            <div class="source-preview">${src.text_preview}</div>
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
            } else if (prov === 'ollama') {
                dom.ollamaUrl.value = s.base_url || 'http://localhost:11434';
    
                // Add model option if not exists and select it
                const modelSelect = dom.ollamaModel;
                let exists = false;
                for (let i = 0; i < modelSelect.options.length; i++) {
                    if (modelSelect.options[i].value === s.model) {
                        exists = true;
                        break;
                    }
                }
                if (!exists) {
                    const opt = document.createElement('option');
                    opt.value = s.model;
                    opt.textContent = s.model;
                    modelSelect.appendChild(opt);
                }
                modelSelect.value = s.model;
            }
            else if (prov === 'gemini') {
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
    saveBtn.innerHTML = '⚡ Validazione...';
    
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
    } else if (provider === 'ollama') {
        payload.base_url = dom.ollamaUrl.value.trim();
        payload.model = dom.ollamaModel.value;
        payload.api_key = null; // Ollama non necessita di api key locale
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
    if (!confirm(`Sei sicuro di voler eliminare la configurazione per ${provider.toUpperCase()}?`)) return;
    
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

async function loadOllamaModels(url) {
    if (!url) return;
    
    dom.btnRefreshOllama.classList.add('loading');
    dom.btnRefreshOllama.disabled = true;
    
    try {
        const res = await apiFetch(`/v1/settings/llm/ollama-models?url=${encodeURIComponent(url)}`);
        const data = await res.json();
        
        const selectElement = dom.ollamaModel;
        const previousValue = selectElement.value;
        selectElement.innerHTML = '';
        
        if (data.models && data.models.length > 0) {
            data.models.forEach(model => {
                const opt = document.createElement('option');
                opt.value = model;
                opt.textContent = model;
                selectElement.appendChild(opt);
            });
            
            // Re-seleziona il precedente se esiste nel nuovo set
            if (data.models.includes(previousValue)) {
                selectElement.value = previousValue;
            }
            showToast('Modelli Ollama aggiornati', 'success');
        } else {
            showToast('Nessun modello trovato sul server Ollama', 'warning');
        }
    } catch (err) {
        console.warn('Impossibile caricare modelli da Ollama URL:', url);
        // Fallback default options
        const selectElement = dom.ollamaModel;
        if (selectElement.options.length === 0) {
            const opt = document.createElement('option');
            opt.value = 'deepseek-r1:14b';
            opt.textContent = 'deepseek-r1:14b (fallback)';
            selectElement.appendChild(opt);
        }
    } finally {
        dom.btnRefreshOllama.classList.remove('loading');
        dom.btnRefreshOllama.disabled = false;
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
