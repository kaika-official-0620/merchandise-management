(function (global) {
    'use strict';

    const pickerStates = new Map();

    function fileKey(file) {
        return [file.name, file.size, file.lastModified, file.type].join(':');
    }

    function formatFileSize(size) {
        if (!Number.isFinite(size) || size <= 0) return '';
        if (size < 1024) return size + ' B';
        if (size < 1024 * 1024) return Math.ceil(size / 1024) + ' KB';
        return (size / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function revokePreviewUrls(state) {
        state.previewUrls.forEach((url) => URL.revokeObjectURL(url));
        state.previewUrls = [];
    }

    function syncInputFiles(state) {
        const transfer = new DataTransfer();
        state.files.forEach((file) => transfer.items.add(file));
        state.syncing = true;
        state.input.files = transfer.files;
        state.syncing = false;
    }

    function renderPicker(state) {
        revokePreviewUrls(state);
        const count = state.files.length;
        state.root.classList.toggle('has-files', count > 0);
        state.count.textContent = count + '枚選択中';
        state.list.replaceChildren();

        state.files.forEach((file, index) => {
            const card = document.createElement('div');
            card.className = 'sale-request-file-card';

            const preview = document.createElement('img');
            const previewUrl = URL.createObjectURL(file);
            state.previewUrls.push(previewUrl);
            preview.src = previewUrl;
            preview.alt = file.name + ' のプレビュー';
            preview.loading = 'lazy';
            preview.addEventListener('error', function () {
                this.hidden = true;
            });

            const details = document.createElement('div');
            details.className = 'sale-request-file-details';

            const name = document.createElement('span');
            name.className = 'sale-request-file-name';
            name.textContent = file.name;

            const size = document.createElement('span');
            size.className = 'sale-request-file-size';
            size.textContent = formatFileSize(file.size);

            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'sale-request-file-remove';
            remove.textContent = '解除';
            remove.setAttribute('aria-label', file.name + ' を選択から解除');
            remove.addEventListener('click', function () {
                state.files.splice(index, 1);
                syncInputFiles(state);
                renderPicker(state);
                state.input.dispatchEvent(new CustomEvent('sale-request-files-changed', {
                    bubbles: true,
                    detail: { count: state.files.length }
                }));
            });

            details.append(name, size);
            card.append(preview, details, remove);
            state.list.appendChild(card);
        });
    }

    function mergeFiles(state, incomingFiles, append) {
        const imageFiles = Array.from(incomingFiles || []).filter((file) => {
            return file && (!file.type || file.type.startsWith('image/'));
        });
        let nextFiles = append ? state.files.concat(imageFiles) : imageFiles;
        const seen = new Set();
        nextFiles = nextFiles.filter((file) => {
            const key = fileKey(file);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
        state.files = state.multiple ? nextFiles : nextFiles.slice(0, 1);
        syncInputFiles(state);
        renderPicker(state);
    }

    function init(inputId) {
        if (pickerStates.has(inputId)) return pickerStates.get(inputId);

        const input = document.getElementById(inputId);
        if (!input) return null;
        const root = input.closest('[data-sale-request-image-picker]');
        if (!root) return null;
        const count = root.querySelector('[data-sale-request-file-count]');
        const list = root.querySelector('[data-sale-request-file-list]');
        if (!count || !list) return null;

        const state = {
            input,
            root,
            count,
            list,
            multiple: input.multiple,
            files: [],
            previewUrls: [],
            syncing: false
        };
        pickerStates.set(inputId, state);

        input.addEventListener('change', function () {
            if (state.syncing) return;
            mergeFiles(state, input.files, state.multiple && state.files.length > 0);
            input.dispatchEvent(new CustomEvent('sale-request-files-changed', {
                bubbles: true,
                detail: { count: state.files.length }
            }));
        });

        ['dragenter', 'dragover'].forEach((eventName) => {
            root.addEventListener(eventName, function (event) {
                event.preventDefault();
                root.classList.add('is-dragging');
            });
        });
        ['dragleave', 'drop'].forEach((eventName) => {
            root.addEventListener(eventName, function (event) {
                event.preventDefault();
                root.classList.remove('is-dragging');
            });
        });
        root.addEventListener('drop', function (event) {
            mergeFiles(state, event.dataTransfer && event.dataTransfer.files, state.multiple);
            input.dispatchEvent(new CustomEvent('sale-request-files-changed', {
                bubbles: true,
                detail: { count: state.files.length }
            }));
        });

        renderPicker(state);
        return state;
    }

    function reset(inputId) {
        const state = pickerStates.get(inputId) || init(inputId);
        if (!state) return;
        state.files = [];
        state.input.value = '';
        renderPicker(state);
    }

    function getCount(inputId) {
        const state = pickerStates.get(inputId) || init(inputId);
        return state ? state.files.length : 0;
    }

    function renderExisting(containerId, paths) {
        const container = document.getElementById(containerId);
        if (!container) return;

        const uniquePaths = Array.from(new Set((paths || []).filter(Boolean)));
        container.replaceChildren();
        container.dataset.hasImage = uniquePaths.length ? 'true' : 'false';
        container.hidden = uniquePaths.length === 0;
        const group = container.closest('.form-group');
        if (group) group.hidden = uniquePaths.length === 0;
        if (!uniquePaths.length) return;

        const summary = document.createElement('p');
        summary.className = 'sale-request-existing-summary';
        summary.textContent = '保存済み ' + uniquePaths.length + '枚';

        const grid = document.createElement('div');
        grid.className = 'sale-request-existing-grid';
        uniquePaths.forEach((path, index) => {
            const link = document.createElement('a');
            link.href = '/static/' + String(path).replace(/^\/+/, '');
            link.target = '_blank';
            link.rel = 'noopener';
            link.className = 'sale-request-existing-card';
            link.setAttribute('aria-label', '保存済み画像' + (index + 1) + 'を拡大表示');

            const image = document.createElement('img');
            image.src = link.href;
            image.alt = '保存済み画像' + (index + 1);
            image.loading = 'lazy';

            const label = document.createElement('span');
            label.textContent = '画像' + (index + 1);
            link.append(image, label);
            grid.appendChild(link);
        });
        container.append(summary, grid);
    }

    global.KaikaSaleRequestImagePicker = {
        init,
        reset,
        getCount,
        renderExisting
    };
})(window);
