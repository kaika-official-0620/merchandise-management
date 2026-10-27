(() => {
  const input = document.getElementById('images');
  const preview = document.getElementById('support-image-preview');
  const form = document.getElementById('support-intake-form');
  let urls = [];
  input?.addEventListener('change', () => {
    urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
    preview.replaceChildren();
    const files = [...input.files];
    const invalid = files.length > 5 || files.some(file => file.size > 5 * 1024 * 1024);
    input.setCustomValidity(invalid ? '画像は5枚まで、1枚5MB以内にしてください。' : '');
    if (invalid) { input.reportValidity(); return; }
    files.forEach((file, index) => {
      const image = document.createElement('img');
      image.alt = `添付する画像 ${index + 1}`;
      image.src = URL.createObjectURL(file); urls.push(image.src);
      preview.append(image);
    });
  });
  form?.addEventListener('submit', () => {
    const submit = document.getElementById('support-submit');
    submit.disabled = true; submit.textContent = '送信しています…';
  });
  window.addEventListener('pageshow', () => {
    const submit = document.getElementById('support-submit');
    if (submit) { submit.disabled = false; submit.textContent = '開花に送信する'; }
  });
})();
