(() => {
  const picker = document.getElementById('intake-product-picker');
  if (!picker) return;
  const search = document.getElementById('intake-product-search');
  const count = document.getElementById('intake-product-count');
  const empty = document.getElementById('intake-product-empty');
  const transfer = document.querySelector('input[name="kind"][value="transfer"]');
  const rows = [...picker.querySelectorAll('[data-intake-choice]')];
  const normalize = value => value.normalize('NFKC').toLocaleLowerCase('ja-JP');
  function update() {
    const terms = normalize(search.value).trim().split(/\s+/).filter(Boolean);
    let visible = 0;
    let selected = 0;
    rows.forEach(row => {
      const matches = terms.every(term => normalize(row.textContent).includes(term));
      row.hidden = !matches;
      row.style.display = matches ? '' : 'none';
      visible += Number(matches);
      selected += Number(row.querySelector('input').checked);
    });
    count.textContent = `${visible} / ${rows.length}点を表示・${selected}点を選択中（1回100点まで）`;
    empty.hidden = visible !== 0;
  }
  search.addEventListener('input', update);
  picker.addEventListener('change', event => {
    if (event.target.matches('input[name="item_ids"]') && event.target.checked) transfer.checked = true;
    update();
  });
  update();
})();
