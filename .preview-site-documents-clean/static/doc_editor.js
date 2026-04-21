document.addEventListener("DOMContentLoaded", () => {
  const editorRoots = document.querySelectorAll("[data-doc-editor]");

  editorRoots.forEach((root) => {
    const table = root.querySelector(".doc-table");
    const totalOutputs = Array.from(root.querySelectorAll("[data-total-output]"));
    const editableSelectors = [
      ".summary-grid .field-value",
      ".doc-recipient-name",
      ".doc-request-text",
      ".doc-company div",
      ".doc-amount-box strong",
      ".doc-meta-list strong",
      ".doc-table tbody td",
      ".doc-notes p",
      ".doc-notes-box li",
      ".doc-page-note"
    ];

    const editableNodes = Array.from(root.querySelectorAll(editableSelectors.join(",")));
    editableNodes.forEach((node) => {
      node.classList.add("doc-editable");
      node.setAttribute("contenteditable", "false");
      node.dataset.initialHtml = node.innerHTML;
    });

    const editButton = root.querySelector("[data-action='toggle-edit']");
    const resetButton = root.querySelector("[data-action='reset-doc']");

    const recalcTotal = () => {
      if (!table || totalOutputs.length === 0) {
        return;
      }

      const selector = table.dataset.sumSelector;
      if (!selector) {
        return;
      }

      const values = Array.from(table.querySelectorAll(`tbody ${selector}`));
      const sum = values.reduce((acc, node) => {
        const text = (node.textContent || "").replace(/[^\d.-]/g, "");
        const num = Number(text);
        return Number.isFinite(num) ? acc + num : acc;
      }, 0);

      totalOutputs.forEach((output) => {
        output.textContent = `¥${sum.toLocaleString("ja-JP")}`;
      });
    };

    editableNodes.forEach((node) => {
      node.addEventListener("blur", recalcTotal);
      node.addEventListener("input", () => {
        if (node.matches(".doc-cell-price, .doc-cell-amount")) {
          recalcTotal();
        }
      });
    });

    if (editButton) {
      editButton.addEventListener("click", () => {
        const nextEditMode = !root.classList.contains("is-edit-mode");
        root.classList.toggle("is-edit-mode", nextEditMode);
        editableNodes.forEach((node) => {
          node.setAttribute("contenteditable", nextEditMode ? "true" : "false");
        });
        editButton.textContent = nextEditMode ? "編集を終了する" : "書類を編集する";
        recalcTotal();
      });
    }

    if (resetButton) {
      resetButton.addEventListener("click", () => {
        editableNodes.forEach((node) => {
          if (node.dataset.initialHtml !== undefined) {
            node.innerHTML = node.dataset.initialHtml;
          }
        });
        recalcTotal();
      });
    }

    recalcTotal();
  });
});
