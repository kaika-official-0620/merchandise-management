function togglePanel(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  panel.hidden = !panel.hidden;
}

function statusClass(label) {
  switch (label) {
    case "認証待ち":
      return "pending";
    case "認証済み":
      return "approved";
    case "査定中":
      return "appraising";
    case "入金待ち":
      return "payment";
    case "受付不可":
      return "unavailable";
    default:
      return "ready";
  }
}

function applyStatus(selectId, badgeId, stateId, noticeId, productName) {
  const select = document.getElementById(selectId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  const notice = document.getElementById(noticeId);
  if (!select || !badge || !state || !notice) return;

  const next = select.value;
  badge.textContent = next;
  badge.className = `pill ${statusClass(next)}`;
  state.textContent = next;
  notice.textContent = `${productName} の状態を「${next}」に更新し、クライアントへ通知する想定です。`;
  notice.hidden = false;
}

function notifyUnavailable(noticeId, badgeId, stateId, productName) {
  const notice = document.getElementById(noticeId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (!notice || !badge || !state) return;

  badge.textContent = "受付不可";
  badge.className = "pill unavailable";
  state.textContent = "受付不可";
  notice.textContent = `${productName} は受付不可としてクライアントへ通知する想定です。`;
  notice.hidden = false;
}

function toggleConfig(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  panel.hidden = !panel.hidden;
}

function saveConfig(inputId, listId, noticeId, label) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(listId);
  const notice = document.getElementById(noticeId);
  if (!input || !list || !notice) return;
  const value = input.value.trim();
  if (!value) {
    notice.textContent = `${label}名を入力してください。`;
    notice.hidden = false;
    return;
  }
  const item = document.createElement("li");
  item.textContent = value;
  list.appendChild(item);
  input.value = "";
  notice.textContent = `${label}「${value}」を登録する想定です。`;
  notice.hidden = false;
}

function syncVendorDraft() {
  const checks = Array.from(document.querySelectorAll(".vendor-check:checked"));
  const subject = document.getElementById("vendor-subject");
  const body = document.getElementById("vendor-body");
  if (!subject || !body) return;

  if (checks.length === 0) {
    subject.value = "";
    body.value = "";
    return;
  }

  const lines = checks.map((checkbox) => checkbox.dataset.product);
  subject.value = `査定依頼：${lines.join("、")}`;
  body.value =
    "下記商品の査定をお願いします。\n\n" +
    lines.map((line, index) => `${index + 1}. ${line}`).join("\n") +
    "\n\n必要に応じて金額やコメントをご記入ください。";
}

function createVendorEstimate() {
  const checks = Array.from(document.querySelectorAll(".vendor-check:checked"));
  const summary = document.getElementById("vendor-created-summary");
  const list = document.getElementById("vendor-created-items");
  const notice = document.getElementById("vendor-create-notice");
  if (!summary || !list || !notice) return;

  if (checks.length === 0) {
    notice.textContent = "書類に載せる商品を選択してください。";
    notice.hidden = false;
    return;
  }

  list.innerHTML = "";
  checks.forEach((checkbox) => {
    const li = document.createElement("li");
    li.textContent = checkbox.dataset.summary;
    list.appendChild(li);
    const card = checkbox.closest(".check-item");
    if (card) card.classList.add("is-hidden");
  });
  summary.hidden = false;
  notice.textContent = "見積依頼書を作成した想定で、選択した商品を一覧から外しました。";
  notice.hidden = false;
}

function restoreVendorEstimate() {
  document.querySelectorAll(".check-item.is-hidden").forEach((card) => {
    card.classList.remove("is-hidden");
  });
  const summary = document.getElementById("vendor-created-summary");
  const notice = document.getElementById("vendor-create-notice");
  if (summary) summary.hidden = true;
  if (notice) {
    notice.textContent = "作成済み書類をキャンセルし、商品を一覧へ戻す想定です。";
    notice.hidden = false;
  }
}

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const monthSelect = document.getElementById("vendor-file-month");
  const notice = document.getElementById("vendor-file-notice");
  if (!fileInput || !monthSelect || !notice) return;

  const fileName = fileInput.value.split("\\").pop();
  if (!fileName) {
    notice.textContent = "登録するファイルを選択してください。";
    notice.hidden = false;
    return;
  }

  notice.textContent = `${monthSelect.value} の業者回答ファイル「${fileName}」を登録する想定です。`;
  notice.hidden = false;
}

function createClientReturn(scope) {
  const checks = Array.from(document.querySelectorAll(`.return-check[data-scope="${scope}"]:checked`));
  const summary = document.getElementById(`${scope}-return-summary`);
  const list = document.getElementById(`${scope}-return-items`);
  const notice = document.getElementById(`${scope}-return-notice`);
  if (!summary || !list || !notice) return;

  if (checks.length === 0) {
    notice.textContent = "送付対象の商品を選択してください。";
    notice.hidden = false;
    return;
  }

  list.innerHTML = "";
  checks.forEach((checkbox) => {
    const amount = document.getElementById(checkbox.dataset.amount);
    const li = document.createElement("li");
    li.textContent = `${checkbox.dataset.product} / 売却額 ${amount ? amount.value || "未入力" : "未入力"} 円`;
    list.appendChild(li);
  });
  summary.hidden = false;
  notice.textContent = "クライアント向け書類を作成し、ユーザー画面へ送付する想定です。";
  notice.hidden = false;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".vendor-check").forEach((checkbox) => {
    checkbox.addEventListener("change", syncVendorDraft);
  });
  syncVendorDraft();
});
