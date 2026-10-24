(function () {
  'use strict';

  function apiBase() {
    const configured =
      typeof URLS !== 'undefined' && URLS && URLS.ai
        ? URLS.ai
        : window.SMART_AI_API_URL || '';

    return String(configured).replace(/\/+$/, '');
  }

  function getToken() {
    return localStorage.getItem('smart_token') || '';
  }

  function headers(json = true) {
    const token = getToken();

    if (!token) {
      throw new Error(
        'No hay sesión de Smart-Account. Inicia sesión otra vez.'
      );
    }

    const result = {
      Authorization: 'Smart-Token ' + token,
      Accept: 'application/json'
    };

    if (json) {
      result['Content-Type'] = 'application/json';
    }

    return result;
  }

  async function request(path, options = {}) {
    const base = apiBase();

    if (!base) {
      throw new Error('Falta URLS.ai en url.js.');
    }

    let response;

    try {
      response = await fetch(base + path, {
        ...options,
        headers: {
          ...headers(options.body !== undefined),
          ...(options.headers || {})
        },
        cache: 'no-store'
      });
    } catch (_) {
      throw new Error('No se pudo conectar con Smart AI API.');
    }

    let data = {};

    try {
      data = await response.json();
    } catch (_) {}

    if (!response.ok) {
      if (response.status === 401) {
        throw new Error(
          'La sesión de Smart-Account no es válida.'
        );
      }

      throw new Error(
        data.detail ||
        data.message ||
        ('Smart AI API respondió con HTTP ' + response.status + '.')
      );
    }

    return data;
  }

  function cleanMessages(messages) {
    return (Array.isArray(messages) ? messages : [])
      .filter(
        m =>
          m &&
          ['user', 'assistant', 'system'].includes(m.role) &&
          typeof m.content === 'string'
      )
      .map(m => ({
        role: m.role,
        content: m.content
      }));
  }

  // Obtener todas las conversaciones del usuario.
  async function loadChats() {
    const data = await request('/api/chats', {
      method: 'GET',
      headers: {}
    });

    return Array.isArray(data.chats) ? data.chats : [];
  }

  // Obtener una conversación concreta.
  async function loadChat(id) {
    const data = await request(
      '/api/chats/' + encodeURIComponent(id),
      {
        method: 'GET',
        headers: {}
      }
    );

    return data.chat;
  }

  // Crear o actualizar una conversación en el servidor.
  async function saveChat(chat) {
    if (!chat || !chat.id) {
      throw new Error('El chat necesita un id estable.');
    }

    const payload = {
      title:
        typeof chat.title === 'string'
          ? chat.title
          : 'Nuevo chat',
      messages: cleanMessages(chat.messages)
    };

    const data = await request(
      '/api/chats/' + encodeURIComponent(chat.id),
      {
        method: 'PUT',
        body: JSON.stringify(payload)
      }
    );

    return data.chat;
  }

  // Eliminar una conversación del servidor.
  async function deleteChat(id) {
    return request(
      '/api/chats/' + encodeURIComponent(id),
      {
        method: 'DELETE',
        headers: {}
      }
    );
  }

  // Obtener la memoria de Smart AI.
  async function loadMemory() {
    const data = await request('/api/memory', {
      method: 'GET',
      headers: {}
    });

    return typeof data.memory === 'string'
      ? data.memory
      : '';
  }

  // Guardar la memoria en el servidor.
  async function saveMemory(memory) {
    const data = await request('/api/memory', {
      method: 'PUT',
      body: JSON.stringify({
        memory: String(memory || '')
      })
    });

    return data.memory || '';
  }

  // Enviar mensajes a Smart AI API.
  async function ask(chat, options = {}) {
    if (!chat || !chat.id) {
      throw new Error(
        'Crea un chat antes de enviar un mensaje.'
      );
    }

    const data = await request('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        chat_id: chat.id,
        messages: cleanMessages(chat.messages),
        summary: String(options.summary || ''),
        memory: String(options.memory || '')
      })
    });

    if (data.chat_id) {
      chat.id = data.chat_id;
    }

    return String(data.reply || '');
  }

  // Buscar mensajes de un contacto en Smart-Chat.
  async function searchPrivateChat(username) {
    const data = await request(
      '/api/integrations/chat/search?target=' +
        encodeURIComponent(username),
      {
        method: 'GET',
        headers: {}
      }
    );

    return Array.isArray(data.messages)
      ? data.messages
      : [];
  }

  // Listar los PDF disponibles en Smart-Cloud.
  async function listCloudPDFs() {
    const data = await request(
      '/api/integrations/cloud/pdfs',
      {
        method: 'GET',
        headers: {}
      }
    );

    return Array.isArray(data.files) ? data.files : [];
  }

  // Migración opcional: eliminar las antiguas claves locales.
  // Llámala solo después de confirmar que los chats ya están
  // guardados correctamente en el servidor.
  function clearLegacyLocalData(username) {
    if (!username) return;

    localStorage.removeItem(
      'sia:' + username + ':chats'
    );

    localStorage.removeItem(
      'sia:' + username + ':memory'
    );
  }

  window.SmartAIStore = Object.freeze({
    loadChats,
    loadChat,
    saveChat,
    deleteChat,
    loadMemory,
    saveMemory,
    ask,
    searchPrivateChat,
    listCloudPDFs,
    clearLegacyLocalData
  });
})();
