const PREVIEW_STORAGE_KEY = "documentsPreviewStateV4";

function previewData() {
  return window.DOCUMENTS_PREVIEW_DATA || { clients: {}, products: {}, vendors: [], responseFiles: [], serviceLabels: {} };
}

function normalizeState(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { items: {}, assignments: {}, vendorDraft: null };
  }
  if (raw.items || raw.assignments || Object.prototype.hasOwnProperty.call(raw, "vendorDraft")) {
    return {
      items: raw.items || {},
      assignments: raw.assignments || {},
      vendorDraft: raw.vendorDraft || null,
    };
  }
  return { items: raw, assignments: {}, vendorDraft: null };
}

function loadPreviewState() {
  try {
    return normalizeState(JSON.parse(window.localStorage.getItem(PREVIEW_STORAGE_KEY) || "{}"));
  } catch (error) {
    return { items: {}, assignments: {}, vendorDraft: null };
  }
}

function savePreviewState(state) {
  window.localStorage.setItem(PREVIEW_STORAGE_KEY, JSON.stringify(state));
}

function getItemState(productId, fallbackStatus = "認証待ち") {
  const state = loadPreviewState();
  return state.items[productId] || { status: fallbackStatus, unavailable: false };
}

function setItemState(productId, patch) {
  const state = loadPreviewState();
  const current = state.items[productId] || {};
  state.items[productId] = { ...current, ...patch };
  savePreviewState(state);
  return state.items[productId];
}

function setVendorDraft(vendorDraft) {
  const state = loadPreviewState();
  state.vendorDraft = vendorDraft;
  savePreviewState(state);
}

function getVendorDraft() {
  return loadPreviewState().vendorDraft || null;
}

function setAssignment(productId, assignment) {
  const state = loadPreviewState();
  state.assignments[productId] = assignment;
  savePreviewState(state);
}

function getAssignments() {
  return loadPreviewState().assignments || {};
}

function defaultAssignments() {
  const assignments = {};
  (previewData().responseFiles || []).forEach((fileInfo) => {
    (fileInfo.items || []).forEach((item) => {
      const selected = findClientByName(item.assigned_client || "");
      if (!selected) return;
      const [clientId, client] = selected;
      assignments[item.product] = {
        clientId,
        clientName: client.name,
        service: fileInfo.service,
        source: fileInfo.label,
        price: item.price,
      };
    });
  });
  return assignments;
}

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name) || "";
}

function showInlineNotice(id, message) {
  const target = document.getElementById(id);
  if (!target) return;
  target.textContent = message;
  target.hidden = false;
}

function statusClass(label) {
  switch (label) {
    case "認証待ち": return "pending";
    case "認証済み": return "approved";
    case "査定中": return "appraising";
    case "出品中": return "listing";
    case "入金待ち": return "payment";
    case "完了": return "completed";
    case "受付不可": return "unavailable";
    default: return "approved";
  }
}

function serviceLabel(service) {
  return previewData().serviceLabels?.[service] || service;
}

function formatYen(value) {
  const amount = Number(value) || 0;
  return `¥${amount.toLocaleString("ja-JP")}`;
}

function findClientByName(name) {
  const normalized = (name || "").trim();
  return Object.entries(previewData().clients || {}).find(([, client]) => client.name === normalized) || null;
}

function buildDocRows(items) {
  const products = previewData().products || {};
  const rows = [];
  for (let index = 0; index < 15; index += 1) {
    const item = items[index];
    if (!item) {
      rows.push(`
        <tr>
          <td class="doc-col-no">&nbsp;</td>
          <td class="doc-col-name"></td>
          <td class="doc-col-brand"></td>
          <td class="doc-col-condition"></td>
          <td class="doc-col-qty"></td>
          <td class="doc-col-price"></td>
          <td class="doc-col-price"></td>
        </tr>
      `);
      continue;
    }
    const product = products[item.product];
    if (!product) continue;
    rows.push(`
      <tr>
        <td class="doc-col-no">${index + 1}</td>
        <td class="doc-col-name">${product.name}</td>
        <td class="doc-col-brand">${product.brand}</td>
        <td class="doc-col-condition">${product.condition}</td>
        <td class="doc-col-qty">1</td>
        <td class="doc-col-price doc-cell-price" data-amount="${item.price}">${formatYen(item.price)}</td>
        <td class="doc-col-price doc-cell-amount" data-amount="${item.price}">${formatYen(item.price)}</td>
      </tr>
    `);
  }
  return rows.join("");
}

function buildReturnGroups() {
  const assignments = { ...defaultAssignments(), ...getAssignments() };
  const groups = {};
  Object.entries(assignments).forEach(([productId, assignment]) => {
    const product = previewData().products?.[productId];
    if (!product) return;
    const current = getItemState(productId, product.status || "認証待ち");
    if (current.unavailable) return;
    const clientId = assignment.clientId || product.client;
    const service = assignment.service || product.service;
    const key = `${clientId}__${service}`;
    if (!groups[key]) {
      groups[key] = {
        key,
        client: clientId,
        service,
        items: [],
      };
    }
    groups[key].items.push({
      product: productId,
      price: Number(assignment.price) || Number(product.amount) || 0,
      source: assignment.source || "回答書類",
    });
  });

  return Object.values(groups).sort((left, right) => {
    const a = `${previewData().clients[left.client]?.name || ""}-${left.service}`;
    const b = `${previewData().clients[right.client]?.name || ""}-${right.service}`;
    return a.localeCompare(b, "ja");
  });
}

function applyStatus(selectId, badgeId, stateId, noticeId, productName, service) {
  const select = document.getElementById(selectId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (!select || !badge || !state) return;

  const next = select.value;
  const card = select.closest("[data-product-id]");
  const productId = card ? card.dataset.productId : "";
  badge.textContent = next;
  badge.className = `pill ${statusClass(next)}`;
  state.textContent = next;
  if (productId) {
    setItemState(productId, { status: next, unavailable: false });
  }
  showInlineNotice(noticeId, `${productName}（${serviceLabel(service)}）の状態を「${next}」へ更新し、クライアントへ通知する想定です。`);
}

function notifyUnavailable(noticeId, badgeId, stateId, cardId, productName) {
  const confirmed = window.confirm(`${productName} を受付不可にして、クライアントへ通知します。よろしいですか？`);
  if (!confirmed) return;
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (badge) {
    badge.textContent = "受付不可";
    badge.className = "pill unavailable";
  }
  if (state) {
    state.textContent = "受付不可";
  }
  const card = document.getElementById(cardId);
  const productId = card ? card.dataset.productId : "";
  if (productId) {
    setItemState(productId, { status: "受付不可", unavailable: true });
  }
  showInlineNotice(noticeId, `${productName} は受付不可としてクライアントへ通知する想定です。`);
  if (card) {
    window.setTimeout(() => {
      card.style.display = "none";
    }, 250);
  }
}

function sendMemo(textareaId, noticeId, productId, productName) {
  const textarea = document.getElementById(textareaId);
  const message = (textarea?.value || "").trim();
  if (!message) {
    showInlineNotice(noticeId, "送信するメモを入力してください。");
    return;
  }
  setItemState(productId, { memo: message });
  showInlineNotice(noticeId, `${productName} についてクライアントへメモを送信する想定です。内容: ${message}`);
}

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const dateInput = document.getElementById("vendor-file-date");
  const serviceInput = document.getElementById("vendor-file-service");
  if (!fileInput) return;
  const fileName = (fileInput.value || "").split("\\").pop();
  if (!fileName) {
    showInlineNotice("vendor-file-notice", "登録する回答ファイルを選択してください。");
    return;
  }
  const dateLabel = dateInput && dateInput.value ? dateInput.value : "未設定";
  const service = serviceInput ? serviceLabel(serviceInput.value) : "業者卸販売";
  showInlineNotice("vendor-file-notice", `${dateLabel} の ${service} 回答ファイル「${fileName}」を登録する想定です。`);
}

function assignClient(inputId, noticeId, productId, productName, price, source, service) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const selected = findClientByName(input.value);
  if (!selected) {
    showInlineNotice(noticeId, "返送先クライアント名を一覧から選択してください。");
    return;
  }
  const [clientId, client] = selected;
  setAssignment(productId, {
    clientId,
    clientName: client.name,
    service,
    source,
    price,
  });
  showInlineNotice(noticeId, `${productName} を「${client.name}」の ${serviceLabel(service)} 返送候補へ振り分けました。4番で確認できます。`);
}

function syncVendorSelectionUI() {
  const rows = Array.from(document.querySelectorAll(".js-vendor-select-row[data-product-id]"));
  const summary = document.getElementById("vendor-selected-summary");
  const empty = document.getElementById("vendor-selection-empty");
  const service = getQueryParam("service") || "wholesale";
  const expectedStatus = service === "simultaneous" ? "出品中" : "査定中";

  rows.forEach((row) => {
    const productId = row.dataset.productId;
    const product = previewData().products?.[productId];
    const current = getItemState(productId, row.dataset.defaultStatus || product?.status || "認証待ち");
    const visible = !current.unavailable && row.dataset.service === service && current.status === expectedStatus;
    row.style.display = visible ? "" : "none";
    const checkbox = row.querySelector(".vendor-check");
    if (!visible && checkbox) checkbox.checked = false;
  });

  if (empty) {
    const visibleRows = rows.filter((row) => row.style.display !== "none");
    empty.hidden = visibleRows.length > 0;
  }

  if (summary) {
    const names = Array.from(document.querySelectorAll(".vendor-check:checked"))
      .map((node) => node.dataset.summary || node.dataset.product)
      .filter(Boolean);
    summary.textContent = names.length ? names.join(" / ") : "まだ商品を選択していません";
  }
}

function prepareEstimateDraft(targetHref) {
  const service = getQueryParam("service") || "wholesale";
  const selectedProducts = Array.from(document.querySelectorAll(".vendor-check:checked"))
    .map((node) => node.dataset.productId)
    .filter(Boolean);
  const vendorSelect = document.getElementById("vendor-target");
  const vendorName = vendorSelect ? vendorSelect.value : "";
  if (!selectedProducts.length) {
    showInlineNotice("vendor-draft-notice", "見積依頼書に入れる商品を1点以上選択してください。");
    return;
  }
  if (!vendorName) {
    showInlineNotice("vendor-draft-notice", "送付先業者を選択してください。");
    return;
  }
  setVendorDraft({
    vendorName,
    selectedProducts,
    service,
    documentType: document.getElementById("document-type")?.value || "",
    createdAt: new Date().toISOString(),
  });
  window.location.href = targetHref;
}

function renderVendorEstimateTemplate() {
  const rowsTarget = document.getElementById("vendor-estimate-rows");
  if (!rowsTarget) return;
  const draft = getVendorDraft();
  const vendorName = document.querySelector("[data-vendor-name]");
  const title = document.querySelector(".doc-title");
  const message = document.querySelector(".doc-message");
  if (!draft || !draft.selectedProducts?.length) {
    rowsTarget.innerHTML = buildDocRows([]);
    if (vendorName) vendorName.textContent = "取引先業者 御中";
    return;
  }
  const items = draft.selectedProducts.map((productId) => {
    const product = previewData().products?.[productId];
    return { product: productId, price: Number(product?.amount) || 0 };
  });
  rowsTarget.innerHTML = buildDocRows(items);
  if (vendorName) vendorName.textContent = `${draft.vendorName} 御中`;
  if (title) {
    title.textContent = draft.service === "auction" ? "オークション依頼書" : (draft.service === "simultaneous" ? "出品管理シート" : "見積依頼書");
  }
  if (message) {
    message.textContent = draft.service === "auction"
      ? "下記の商品について、オークション出品のご確認をお願いいたします。"
      : (draft.service === "simultaneous"
          ? "下記の商品について、同時出品の管理内容をご確認ください。"
          : "下記の商品について、見積のご確認をお願いいたします。");
  }
  const total = items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
}

function renderStage4Summary() {
  const container = document.getElementById("client-outgoing-groups");
  if (!container) return;
  const groups = buildReturnGroups();
  const empty = document.getElementById("client-outgoing-empty");
  if (!groups.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  const byClient = {};
  groups.forEach((group) => {
    if (!byClient[group.client]) byClient[group.client] = [];
    byClient[group.client].push(group);
  });
  container.innerHTML = Object.entries(byClient).map(([clientId, clientGroups]) => {
    const client = previewData().clients?.[clientId];
    const serviceCards = clientGroups.map((group) => {
      const total = group.items.reduce((sum, item) => sum + item.price, 0);
      const names = group.items.map((item) => previewData().products?.[item.product]?.name).filter(Boolean).join(" / ");
      const query = encodeURIComponent(group.key);
      return `
        <div class="compact-item">
          <strong>${serviceLabel(group.service)}</strong>
          <span>${group.items.length}点 / ${names}</span>
          <span>合計予定金額 ${formatYen(total)}</span>
          <div class="card-actions">
            <a class="btn btn-soft" href="documents_v2_client_delivery.html?group=${query}">返送内容を確認する</a>
            <a class="btn btn-primary" href="documents_v2_client_statement_template.html?group=${query}">買取明細書を作成する</a>
          </div>
        </div>
      `;
    }).join("");
    return `
      <div class="summary-card">
        <div class="summary-head">
          <div>
            <div class="summary-client">${client?.name || "クライアント"}</div>
            <div class="summary-meta">クライアントごとに、業者卸販売 / 業者オークション / 同時出品を分けて返送書類を作成します。</div>
          </div>
          <span class="pill payment">返送準備中</span>
        </div>
        <div class="compact-list" style="margin-top:14px;">${serviceCards}</div>
      </div>
    `;
  }).join("");
}

function getGroupFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const key = params.get("group");
  if (!key) return null;
  return buildReturnGroups().find((group) => group.key === key) || null;
}

function renderClientDelivery() {
  const container = document.getElementById("client-delivery-items");
  if (!container) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("client-delivery-empty");
  const title = document.getElementById("client-delivery-title");
  const description = document.getElementById("client-delivery-description");
  const templateLink = document.getElementById("client-delivery-template-link");
  if (!group) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} / ${serviceLabel(group.service)} の返送内容`;
  if (description) description.textContent = `ここでどの商品を返送書類に含めるかを確認し、${serviceLabel(group.service)} 用の買取明細書へ進みます。`;
  if (templateLink) templateLink.href = `documents_v2_client_statement_template.html?group=${encodeURIComponent(group.key)}`;
  if (empty) empty.hidden = group.items.length > 0;
  container.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const detailHref = product.real_item_id ? `/view/${product.real_item_id}` : product.detail_page;
    return `
      <div class="product-card js-product-card" data-product-id="${item.product}" data-service="${product.service}" data-default-status="${product.status}" data-stage="client-outgoing">
        <div class="product-thumb">
          <img src="${product.image}" alt="${product.name}">
        </div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${serviceLabel(product.service)} / ${item.source}</div>
            </div>
            <span class="pill payment">返送予定</span>
          </div>
          <div class="detail-grid">
            <div class="field-block"><div class="field-label">売却額</div><div class="field-value">${formatYen(item.price)}</div></div>
            <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="${detailHref}">商品詳細を見る</a></div></div>
            <div class="field-block"><div class="field-label">返送書類</div><div class="field-value">買取明細書へ反映</div></div>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

function renderStatementTemplate() {
  const rowsTarget = document.getElementById("client-statement-rows");
  if (!rowsTarget) return;
  const group = getGroupFromQuery();
  const clientName = document.querySelector("[data-client-name]");
  const clientService = document.querySelector("[data-client-service]");
  const backLink = document.getElementById("statement-back-link");
  if (!group) {
    rowsTarget.innerHTML = buildDocRows([]);
    return;
  }
  const client = previewData().clients?.[group.client];
  if (clientName) clientName.textContent = client?.name || "クライアント名";
  if (clientService) clientService.textContent = serviceLabel(group.service);
  if (backLink) backLink.href = `documents_v2_client_delivery.html?group=${encodeURIComponent(group.key)}`;
  rowsTarget.innerHTML = buildDocRows(group.items);
  const total = group.items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
}

function renderBatchCreateMeta() {
  const title = document.getElementById("batch-create-title");
  if (!title) return;
  const copy = document.getElementById("batch-create-copy");
  const listTitle = document.getElementById("batch-create-list-title");
  const configTitle = document.getElementById("batch-create-config-title");
  const targetLabel = document.getElementById("batch-target-label");
  const docType = document.getElementById("document-type");
  const service = getQueryParam("service") || "wholesale";
  const config = {
    wholesale: {
      title: "業者販売の書類を作成する",
      copy: "業者販売へ流す商品を複数選択し、見積依頼書テンプレートへまとめて差し込みます。",
      listTitle: "業者販売へ流す商品",
      configTitle: "見積依頼書の設定",
      targetLabel: "送付先業者",
      docType: "見積依頼書",
    },
    auction: {
      title: "オークション依頼書を作成する",
      copy: "オークションへ出す商品を複数選択し、オークション依頼書テンプレートへ差し込みます。",
      listTitle: "オークションへ出す商品",
      configTitle: "オークション依頼書の設定",
      targetLabel: "送付先業者",
      docType: "オークション依頼書",
    },
    simultaneous: {
      title: "同時出品の管理書類を作成する",
      copy: "同時出品の商品を選択し、出品管理シートへ差し込みます。",
      listTitle: "同時出品で扱う商品",
      configTitle: "出品管理シートの設定",
      targetLabel: "出品先",
      docType: "出品管理シート",
    },
  }[service];
  if (!config) return;
  title.textContent = config.title;
  if (copy) copy.textContent = config.copy;
  if (listTitle) listTitle.textContent = config.listTitle;
  if (configTitle) configTitle.textContent = config.configTitle;
  if (targetLabel) targetLabel.textContent = config.targetLabel;
  if (docType) docType.value = config.docType;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".js-product-card[data-product-id]").forEach((card) => {
    const productId = card.dataset.productId;
    const product = previewData().products?.[productId];
    const fallbackStatus = card.dataset.defaultStatus || product?.status || "認証待ち";
    const current = getItemState(productId, fallbackStatus);
    const badge = card.querySelector(`[id^="badge-"]`);
    const stateTarget = card.querySelector(`[id^="state-"]`);
    const select = card.querySelector("select.status-select");

    if (badge) {
      badge.textContent = current.status || fallbackStatus;
      badge.className = `pill ${statusClass(current.status || fallbackStatus)}`;
    }
    if (stateTarget) {
      stateTarget.textContent = current.status || fallbackStatus;
    }
    if (select) {
      select.value = current.status || fallbackStatus;
    }

    if (current.unavailable) {
      card.style.display = "none";
    }

    if (card.dataset.stage && card.dataset.stage.startsWith("outgoing-")) {
      const expected = card.dataset.expectedStatus || "査定中";
      if ((current.status || fallbackStatus) !== expected || current.unavailable) {
        card.style.display = "none";
      }
    }
  });

  ["wholesale", "auction", "simultaneous"].forEach((service) => {
    const cards = Array.from(document.querySelectorAll(`.js-product-card[data-stage="outgoing-${service}"]`)).filter((card) => card.style.display !== "none");
    const empty = document.getElementById(`outgoing-empty-${service}`);
    if (empty) empty.hidden = cards.length > 0;
  });

  document.querySelectorAll(".vendor-check").forEach((checkbox) => {
    checkbox.addEventListener("change", syncVendorSelectionUI);
  });

  document.querySelectorAll(".assign-input[data-product-id]").forEach((input) => {
    const assignment = getAssignments()[input.dataset.productId];
    if (assignment?.clientName) {
      input.value = assignment.clientName;
    }
  });

  syncVendorSelectionUI();
  renderBatchCreateMeta();
  renderVendorEstimateTemplate();
  renderStage4Summary();
  renderClientDelivery();
  renderStatementTemplate();
});
