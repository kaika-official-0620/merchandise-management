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
    case "入金待ち": return "payment";
    case "受付不可": return "unavailable";
    default: return "approved";
  }
}

function applyStatus(selectId, badgeId, stateId, noticeId, productName) {
  const select = document.getElementById(selectId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (!select || !badge || !state) return;
  const next = select.value;
  badge.textContent = next;
  badge.className = `pill ${statusClass(next)}`;
  state.textContent = next;
  showInlineNotice(noticeId, `${productName} の状態を「${next}」へ更新し、クライアントへ通知する想定です。`);
}

function notifyUnavailable(noticeId, badgeId, stateId, productName) {
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (badge) {
    badge.textContent = "受付不可";
    badge.className = "pill unavailable";
  }
  if (state) {
    state.textContent = "受付不可";
  }
  showInlineNotice(noticeId, `${productName} は受付不可としてクライアントへ通知する想定です。`);
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
