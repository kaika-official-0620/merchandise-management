async function downloadDocumentAsWord(filename) {
  if (typeof downloadElementAsWord !== "function") return false;
  return downloadElementAsWord(document.querySelector(".doc-page"), filename);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-doc-editor]").forEach((root) => {
    const editableNodes = Array.from(root.querySelectorAll(".doc-meta strong, .doc-meta div, .doc-message, .doc-table tbody td, .doc-note p"));
    editableNodes.forEach((node) => {
      node.classList.add("doc-editable");
      node.dataset.initialHtml = node.innerHTML;
      node.setAttribute("contenteditable", "false");
    });

    const totalOutputs = Array.from(root.querySelectorAll("[data-total-output]"));
    const editButton = root.parentElement.querySelector("[data-action='toggle-edit']");
    const resetButton = root.parentElement.querySelector("[data-action='reset-doc']");

    const formatYen = (value) => `¥${value.toLocaleString("ja-JP")}`;

    function recalcTotal() {
      const amountCells = Array.from(root.querySelectorAll(".doc-cell-amount"));
      const total = amountCells.reduce((sum, cell) => {
        const text = (cell.textContent || "").replace(/[^\d.-]/g, "");
        const value = Number(text);
        return Number.isFinite(value) ? sum + value : sum;
      }, 0);
      totalOutputs.forEach((output) => {
        output.textContent = formatYen(total);
      });
    }

    editableNodes.forEach((node) => {
      node.addEventListener("input", recalcTotal);
      node.addEventListener("blur", recalcTotal);
    });

    if (editButton) {
      editButton.addEventListener("click", () => {
        const editing = !root.classList.contains("is-edit-mode");
        root.classList.toggle("is-edit-mode", editing);
        editableNodes.forEach((node) => {
          node.setAttribute("contenteditable", editing ? "true" : "false");
        });
        editButton.textContent = editing ? "編集を終了する" : "書類を編集する";
      });
    }

    if (resetButton) {
      resetButton.addEventListener("click", () => {
        editableNodes.forEach((node) => {
          node.innerHTML = node.dataset.initialHtml || "";
        });
        recalcTotal();
      });
    }

    recalcTotal();
  });
});
