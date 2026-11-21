(() => {
    const issueDate = document.getElementById('issue_date');
    const expiryDate = document.getElementById('valid_until');

    function syncExpiryDate() {
        expiryDate.min = issueDate.value;
        expiryDate.setCustomValidity(expiryDate.validity.rangeUnderflow
            ? '期限は発行日以降の日付を入力してください。'
            : '');
    }

    issueDate.addEventListener('input', syncExpiryDate);
    expiryDate.addEventListener('input', syncExpiryDate);
    syncExpiryDate();
})();
