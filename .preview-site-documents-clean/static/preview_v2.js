const PREVIEW_STORAGE_KEY = "documentsPreviewStateV3";

function loadPreviewState() {
  try {
    return JSON.parse(window.localStorage.getItem(PREVIEW_STORAGE_KEY) || "{}");
  } catch (error) {
    return {};
  }
}

function savePreviewState(state) {
  window.localStorage.setItem(PREVIEW_STORAGE_KEY, JSON.stringify(state));
}

function getItemState(productId, fallbackStatus = "認証待ち") {
  const state = loadPreviewState();
  return state[productId] || { status: fallbackStatus, unavailable: false };
}

function setItemState(productId, patch) {
  const state = loadPreviewState();
  const current = state[productId] || {};
  state[productId] = { ...current, ...patch };
  savePreviewState(state);
  return state[productId];
}

function togglePanel(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  panel.hidden = !panel.hidden;
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
  const serviceLabel = service === "simultaneous" ? "同時出品" : (service === "auction" ? "業者オークション" : "業者卸販売");
  showInlineNotice(noticeId, `${productName}（${serviceLabel}）の状態を「${next}」へ更新し、クライアントへ通知する想定です。`);
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

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const dateInput = document.getElementById("vendor-file-date");
  if (!fileInput) return;
  const fileName = (fileInput.value || "").split("\\").pop();
  if (!fileName) {
    showInlineNotice("vendor-file-notice", "登録する回答ファイルを選択してください。");
    return;
  }
  const dateLabel = dateInput && dateInput.value ? dateInput.value : "未設定";
  showInlineNotice("vendor-file-notice", `${dateLabel} の回答ファイル「${fileName}」を登録する想定です。`);
}

function assignClient(inputId, noticeId, productName) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const client = input.value || "未選択";
  showInlineNotice(noticeId, `${productName} を「${client}」の返送候補へ振り分ける想定です。`);
}

document.addEventListener("DOMContentLoaded", () => {
  const state = loadPreviewState();

  document.querySelectorAll(".js-product-card[data-product-id]").forEach((card) => {
    const productId = card.dataset.productId;
    const fallbackStatus = card.dataset.defaultStatus || "認証待ち";
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

    if (card.dataset.stage === "vendor-outgoing") {
      const expected = card.dataset.expectedStatus || "査定中";
      if ((current.status || fallbackStatus) !== expected || current.unavailable) {
        card.style.display = "none";
      }
    }
  });

  const visibleVendorCards = Array.from(document.querySelectorAll('.js-product-card[data-stage="vendor-outgoing"]')).filter((card) => card.style.display !== "none");
  const vendorEmpty = document.getElementById("vendor-outgoing-empty");
  if (vendorEmpty) {
    vendorEmpty.hidden = visibleVendorCards.length > 0;
  }

  document.querySelectorAll(".vendor-check").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const names = Array.from(document.querySelectorAll(".vendor-check:checked")).map((node) => node.dataset.product);
      const summary = document.getElementById("vendor-selected-summary");
      if (summary) {
        summary.textContent = names.length ? names.join(" / ") : "まだ商品を選択していません";
      }
    });
  });
});
