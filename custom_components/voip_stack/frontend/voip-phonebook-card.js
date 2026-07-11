const DEFAULT_ENTITY = "sensor.voip_phonebook";

class VoipPhonebookCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._lastRoster = null;
  }

  static getStubConfig() {
    return { entity: DEFAULT_ENTITY, title: "VoIP Phonebook" };
  }

  static getConfigForm() {
    const labels = {
      entity: "Phonebook entity",
      title: "Title",
      empty_text: "Empty phonebook text",
      show_disabled: "Show disabled contacts",
    };
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { filter: { domain: "sensor" } } } },
        { name: "title", selector: { text: {} } },
        { name: "empty_text", selector: { text: {} } },
        { name: "show_disabled", selector: { boolean: {} } },
      ],
      computeLabel: (schema) => labels[schema.name],
      assertConfig: (config) => VoipPhonebookCard._assertConfig(config),
    };
  }

  static _assertConfig(config) {
    const entity = config?.entity || DEFAULT_ENTITY;
    if (typeof entity !== "string" || !entity.startsWith("sensor.")) {
      throw new Error("VoIP Phonebook requires a sensor entity");
    }
  }

  getGridOptions() {
    return { columns: 12, rows: 7, min_columns: 4, min_rows: 3 };
  }

  getCardSize() {
    return 6;
  }

  setConfig(config) {
    VoipPhonebookCard._assertConfig(config);
    this._config = { entity: DEFAULT_ENTITY, title: "VoIP Phonebook", ...config };
    this._lastRoster = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const entity = this._config.entity || DEFAULT_ENTITY;
    const roster = hass?.states?.[entity]?.attributes?.roster_json || "";
    if (roster === this._lastRoster) return;
    this._lastRoster = roster;
    this._render();
  }

  _contacts() {
    if (!this._hass) return [];
    const entity = this._config.entity || DEFAULT_ENTITY;
    const raw = this._hass.states?.[entity]?.attributes?.roster_json;
    if (!raw) return [];
    try {
      const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
      const contacts = Array.isArray(parsed) ? parsed : parsed?.contacts || parsed?.entries || [];
      return contacts
        .filter((contact) => contact && (this._config.show_disabled || contact.enabled !== false))
        .sort((a, b) => this._name(a).localeCompare(this._name(b), undefined, { sensitivity: "base" }));
    } catch (_) {
      return [];
    }
  }

  _name(contact) {
    return String(contact?.name || contact?.id || "Unnamed");
  }

  _appendContact(list, contact) {
    const row = document.createElement("div");
    row.className = "contact";

    row.setAttribute("role", "listitem");
    const heading = document.createElement("div");
    heading.className = "contact-heading";
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", contact.number ? "mdi:phone-classic" : "mdi:account-voice");
    const name = document.createElement("span");
    name.className = "contact-name";
    name.textContent = this._name(contact);
    heading.append(icon, name);
    row.appendChild(heading);

    const details = document.createElement("div");
    details.className = "details";
    if (contact.extension) {
      const extension = document.createElement("div");
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "↳";
      const label = document.createElement("span");
      label.textContent = "Extension:";
      const value = document.createElement("code");
      value.textContent = String(contact.extension);
      extension.append(arrow, label, value);
      details.appendChild(extension);
    }
    if (contact.number) {
      const number = document.createElement("div");
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "↳";
      const label = document.createElement("span");
      label.textContent = "Number:";
      const link = document.createElement("a");
      link.href = `tel:${String(contact.number).replace(/[^\d+*#]/g, "")}`;
      link.textContent = String(contact.number);
      number.append(arrow, label, link);
      details.appendChild(number);
    }
    if (details.childElementCount) row.appendChild(details);
    list.appendChild(row);
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.replaceChildren();

    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; height: 100%; min-height: 0; }
      ha-card {
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 190px;
        overflow: hidden;
      }
      .header {
        flex: 0 0 auto;
        padding: 18px 18px 10px;
        font-size: 1.35rem;
        font-weight: 500;
        line-height: 1.25;
      }
      .list {
        flex: 1 1 auto;
        min-height: 0;
        overflow-x: hidden;
        overflow-y: auto;
        padding: 4px 18px 16px;
        scrollbar-gutter: stable;
      }
      .contact { padding: 8px 0; }
      .contact + .contact { border-top: 1px solid var(--divider-color); }
      .contact-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
      .contact-heading ha-icon { --mdc-icon-size: 22px; flex: 0 0 auto; }
      .contact-name { overflow: hidden; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
      .details { margin: 5px 0 0 30px; color: var(--secondary-text-color); font-size: .9rem; }
      .details > div { display: flex; align-items: baseline; gap: 6px; min-width: 0; padding: 2px 0; }
      .arrow { color: var(--secondary-text-color); }
      code {
        color: var(--primary-text-color);
        background: transparent;
        padding: 0;
        font-family: inherit;
      }
      a { color: var(--primary-color); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .empty { color: var(--secondary-text-color); font-style: italic; padding: 12px 0; }
    `;

    const card = document.createElement("ha-card");
    const title = document.createElement("div");
    title.className = "header";
    title.textContent = this._config.title || "VoIP Phonebook";
    card.appendChild(title);

    const list = document.createElement("div");
    list.className = "list";
    list.setAttribute("role", "list");
    list.setAttribute("aria-label", this._config.title || "VoIP Phonebook");
    list.tabIndex = 0;
    const contacts = this._contacts();
    if (!contacts.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = this._config.empty_text || "No contacts available.";
      list.appendChild(empty);
    } else {
      contacts.forEach((contact) => this._appendContact(list, contact));
    }
    card.appendChild(list);
    this.shadowRoot.append(style, card);
  }
}

if (!customElements.get("voip-phonebook-card")) {
  customElements.define("voip-phonebook-card", VoipPhonebookCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "voip-phonebook-card")) {
  window.customCards.push({
    type: "voip-phonebook-card",
    name: "VoIP Phonebook",
    description: "Scrollable live view of the VoIP Stack roster.",
    preview: true,
    documentationURL: "https://github.com/n-IA-hane/esphome-intercom#lovelace-card",
    getEntitySuggestion: (_hass, entityId) => entityId === DEFAULT_ENTITY
      ? { config: { type: "custom:voip-phonebook-card", entity: entityId } }
      : null,
  });
}
