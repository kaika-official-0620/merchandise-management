(function () {
    'use strict';

    const CONFIG_URL = '/api/google-drive/config';
    const DOWNLOAD_URL = '/api/google-drive/download';
    const MAX_ADDITIONAL_IMAGES = 19;
    const SCRIPT_TIMEOUT_MS = 15000;

    function loadScript(id, src, ready) {
        if (ready()) return Promise.resolve();
        const existing = document.getElementById(id);
        return new Promise((resolve, reject) => {
            let settled = false;
            const finish = (error) => {
                if (settled) return;
                settled = true;
                window.clearTimeout(timer);
                if (error) reject(error);
                else resolve();
            };
            const timer = window.setTimeout(
                () => finish(new Error('Google APIの読込がタイムアウトしました。')),
                SCRIPT_TIMEOUT_MS
            );
            const script = existing || document.createElement('script');
            script.addEventListener('load', () => {
                if (ready()) finish();
                else finish(new Error('Google APIを初期化できませんでした。'));
            }, { once: true });
            script.addEventListener('error', () => finish(new Error('Google APIを読み込めませんでした。')), { once: true });
            if (!existing) {
                script.id = id;
                script.src = src;
                script.async = true;
                script.defer = true;
                document.head.appendChild(script);
            }
        });
    }

    function buildDriveIcon() {
        const wrapper = document.createElement('span');
        wrapper.setAttribute('aria-hidden', 'true');
        wrapper.textContent = 'Drive';
        return wrapper;
    }

    function createPreviewCard(entry, remove) {
        const card = document.createElement('div');
        card.className = 'kaika-image-picker__card';

        const image = document.createElement('img');
        entry.previewUrl = URL.createObjectURL(entry.file);
        image.src = entry.previewUrl;
        image.alt = entry.file.name || '選択した画像';

        const name = document.createElement('span');
        name.className = 'kaika-image-picker__name';
        name.textContent = entry.file.name || '画像';

        const source = document.createElement('span');
        source.className = 'kaika-image-picker__source';
        if (entry.source === 'drive') {
            source.classList.add('kaika-image-picker__source--drive');
            source.textContent = 'Google Drive';
        } else {
            source.textContent = 'PC / 端末';
        }

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'kaika-image-picker__remove';
        removeButton.setAttribute('aria-label', `${entry.file.name || '画像'}を選択解除`);
        removeButton.textContent = '×';
        removeButton.addEventListener('click', remove);

        card.append(image, name, source, removeButton);
        return card;
    }

    function revokePreview(entry) {
        if (entry && entry.previewUrl) {
            URL.revokeObjectURL(entry.previewUrl);
            entry.previewUrl = '';
        }
    }

    function init(options) {
        const root = options && options.root;
        const form = options && options.form;
        if (!root || !form || root.dataset.initialized === 'true') return null;
        root.dataset.initialized = 'true';

        const mainInput = document.getElementById('photo');
        const additionalInput = document.getElementById('additional_photos');
        const mainPreview = document.getElementById('kaika-main-image-preview');
        const additionalPreview = document.getElementById('kaika-additional-image-preview');
        const mainDriveButton = document.getElementById('google-drive-photo-btn');
        const additionalDriveButton = document.getElementById('google-drive-additional-btn');
        const status = document.getElementById('google-drive-picker-status');
        const count = document.getElementById('kaika-additional-image-count');

        if (!mainInput || !additionalInput || !mainPreview || !additionalPreview) return null;

        let mainEntry = null;
        let localAdditionalEntries = [];
        let driveAdditionalEntries = [];
        let config = null;
        let accessToken = '';
        let accessTokenExpiresAt = 0;
        let tokenClient = null;
        let pickerReadyPromise = null;
        let googleScriptsPromise = null;

        function setStatus(message, level) {
            if (!status) return;
            status.textContent = message || '';
            status.dataset.level = level || 'info';
        }

        function existingAdditionalCount() {
            return Array.from(document.querySelectorAll('input[name="delete_photos"]'))
                .filter((checkbox) => !checkbox.checked)
                .length;
        }

        function selectedAdditionalCount() {
            return existingAdditionalCount() + localAdditionalEntries.length + driveAdditionalEntries.length;
        }

        function availableAdditionalSlots() {
            return Math.max(0, MAX_ADDITIONAL_IMAGES - selectedAdditionalCount());
        }

        function updateCount() {
            if (!count) return;
            count.textContent = `選択中 ${selectedAdditionalCount()} / ${MAX_ADDITIONAL_IMAGES}枚（既存画像を含む）`;
        }

        function syncLocalAdditionalInput() {
            if (typeof DataTransfer === 'undefined') return;
            const transfer = new DataTransfer();
            localAdditionalEntries.forEach((entry) => transfer.items.add(entry.file));
            additionalInput.files = transfer.files;
        }

        function renderMain() {
            mainPreview.innerHTML = '';
            if (!mainEntry) return;
            mainPreview.appendChild(createPreviewCard(mainEntry, () => {
                revokePreview(mainEntry);
                if (mainEntry.source === 'local') mainInput.value = '';
                mainEntry = null;
                renderMain();
                setStatus('メイン画像の選択を解除しました。', 'info');
            }));
        }

        function renderAdditional() {
            additionalPreview.querySelectorAll('.kaika-image-picker__card').forEach((card) => card.remove());
            const entries = localAdditionalEntries.concat(driveAdditionalEntries);
            entries.forEach((entry) => {
                additionalPreview.appendChild(createPreviewCard(entry, () => {
                    revokePreview(entry);
                    if (entry.source === 'drive') {
                        driveAdditionalEntries = driveAdditionalEntries.filter((candidate) => candidate !== entry);
                    } else {
                        localAdditionalEntries = localAdditionalEntries.filter((candidate) => candidate !== entry);
                        syncLocalAdditionalInput();
                    }
                    renderAdditional();
                    updateCount();
                    setStatus(`${entry.file.name} の選択を解除しました。`, 'info');
                }));
            });
            updateCount();
        }

        function selectedDriveIds() {
            const ids = new Set(driveAdditionalEntries.map((entry) => entry.driveId));
            if (mainEntry && mainEntry.source === 'drive') ids.add(mainEntry.driveId);
            return ids;
        }

        mainInput.addEventListener('change', () => {
            const file = mainInput.files && mainInput.files[0];
            revokePreview(mainEntry);
            mainEntry = file ? { file, source: 'local', previewUrl: '' } : null;
            renderMain();
            if (file) setStatus(`${file.name} をPC / 端末から選択しました。`, 'success');
        });

        additionalInput.addEventListener('change', () => {
            localAdditionalEntries.forEach(revokePreview);
            const selectedFiles = Array.from(additionalInput.files || []);
            const maximumLocalCount = Math.max(
                0,
                MAX_ADDITIONAL_IMAGES - existingAdditionalCount() - driveAdditionalEntries.length
            );
            const accepted = selectedFiles.slice(0, maximumLocalCount);
            localAdditionalEntries = accepted.map((file) => ({ file, source: 'local', previewUrl: '' }));
            syncLocalAdditionalInput();
            renderAdditional();
            if (accepted.length < selectedFiles.length) {
                setStatus(`追加画像は既存画像を含めて最大${MAX_ADDITIONAL_IMAGES}枚です。`, 'error');
            } else if (accepted.length) {
                setStatus(`${accepted.length}枚をPC / 端末から選択しました。`, 'success');
            }
        });

        document.querySelectorAll('input[name="delete_photos"]').forEach((checkbox) => {
            checkbox.addEventListener('change', updateCount);
        });

        async function loadGoogleScripts() {
            if (googleScriptsPromise) return googleScriptsPromise;
            googleScriptsPromise = Promise.all([
                loadScript(
                    'google-identity-services-script',
                    'https://accounts.google.com/gsi/client',
                    () => Boolean(window.google && window.google.accounts && window.google.accounts.oauth2)
                ),
                loadScript(
                    'google-picker-api-script',
                    'https://apis.google.com/js/api.js',
                    () => Boolean(window.gapi)
                ),
            ]).then(() => {
                if (!pickerReadyPromise) {
                    pickerReadyPromise = new Promise((resolve, reject) => {
                        const timer = window.setTimeout(
                            () => reject(new Error('Google Pickerの初期化がタイムアウトしました。')),
                            SCRIPT_TIMEOUT_MS
                        );
                        window.gapi.load('picker', {
                            callback: () => {
                                window.clearTimeout(timer);
                                resolve();
                            },
                            onerror: () => {
                                window.clearTimeout(timer);
                                reject(new Error('Google Pickerを初期化できませんでした。'));
                            },
                        });
                    });
                }
                return pickerReadyPromise;
            });
            return googleScriptsPromise;
        }

        function ensureTokenClient() {
            if (tokenClient) return;
            tokenClient = window.google.accounts.oauth2.initTokenClient({
                client_id: config.clientId,
                scope: config.scope,
                callback: function () {},
                error_callback: function () {},
            });
        }

        function requestAccessToken() {
            if (accessToken && Date.now() < accessTokenExpiresAt) {
                return Promise.resolve(accessToken);
            }
            ensureTokenClient();
            return new Promise((resolve, reject) => {
                tokenClient.callback = (response) => {
                    if (!response || response.error || !response.access_token) {
                        accessToken = '';
                        reject(new Error('Google認証が完了しませんでした。'));
                        return;
                    }
                    accessToken = response.access_token;
                    accessTokenExpiresAt = Date.now() + Math.max(0, Number(response.expires_in || 0) - 60) * 1000;
                    resolve(accessToken);
                };
                tokenClient.error_callback = (error) => {
                    const errorType = error && error.type;
                    if (errorType === 'popup_closed' || errorType === 'popup_failed_to_open') {
                        reject(new Error('Google認証をキャンセルしました。'));
                    } else {
                        reject(new Error('Google認証を開始できませんでした。'));
                    }
                };
                tokenClient.requestAccessToken({ prompt: accessToken ? '' : 'consent' });
            });
        }

        async function readError(response) {
            try {
                const body = await response.json();
                return body.error || 'Google Drive画像を取得できませんでした。';
            } catch (_error) {
                return 'Google Drive画像を取得できませんでした。';
            }
        }

        async function fetchDriveImage(documentInfo) {
            const response = await fetch(DOWNLOAD_URL, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ fileId: documentInfo.id }),
            });
            if (!response.ok) {
                if (response.status === 401) {
                    accessToken = '';
                    accessTokenExpiresAt = 0;
                }
                throw new Error(await readError(response));
            }
            const receipt = response.headers.get('X-Google-Drive-Receipt') || '';
            const driveId = response.headers.get('X-Google-Drive-File-Id') || '';
            const encodedName = response.headers.get('X-Google-Drive-File-Name') || '';
            if (!receipt || !driveId) throw new Error('Drive画像の確認情報を取得できませんでした。');
            let fileName = 'drive-image';
            try {
                fileName = decodeURIComponent(encodedName) || fileName;
            } catch (_error) {
                fileName = 'drive-image';
            }
            const blob = await response.blob();
            const file = new File([blob], fileName, {
                type: blob.type || 'application/octet-stream',
                lastModified: Date.now(),
            });
            return { file, source: 'drive', driveId, receipt, previewUrl: '' };
        }

        async function handlePicked(target, docs) {
            const driveIds = selectedDriveIds();
            let added = 0;
            const activeButton = target === 'main' ? mainDriveButton : additionalDriveButton;
            if (activeButton) activeButton.disabled = true;
            try {
                for (const documentInfo of docs) {
                    if (!documentInfo || !documentInfo.id || driveIds.has(documentInfo.id)) {
                        setStatus('同じGoogle Drive画像は重複して追加できません。', 'error');
                        continue;
                    }
                    if (target === 'additional' && availableAdditionalSlots() <= 0) {
                        setStatus(`追加画像は既存画像を含めて最大${MAX_ADDITIONAL_IMAGES}枚です。`, 'error');
                        break;
                    }
                    const entry = await fetchDriveImage(documentInfo);
                    if (driveIds.has(entry.driveId)) {
                        setStatus('同じGoogle Drive画像は重複して追加できません。', 'error');
                        continue;
                    }
                    driveIds.add(entry.driveId);
                    if (target === 'main') {
                        revokePreview(mainEntry);
                        mainInput.value = '';
                        mainEntry = entry;
                        renderMain();
                        added = 1;
                        break;
                    }
                    driveAdditionalEntries.push(entry);
                    added += 1;
                    renderAdditional();
                }
                if (added) {
                    setStatus(
                        target === 'main'
                            ? `${mainEntry.file.name} をGoogle Driveから選択しました。`
                            : `${added}枚をGoogle Driveから追加しました。`,
                        'success'
                    );
                }
            } finally {
                if (activeButton && config && config.enabled) activeButton.disabled = false;
            }
        }

        async function openPicker(target) {
            try {
                setStatus('Google Driveを読み込んでいます…', 'info');
                await loadGoogleScripts();
                await requestAccessToken();
                const remaining = target === 'main' ? 1 : availableAdditionalSlots();
                if (remaining <= 0) {
                    setStatus(`追加画像は既存画像を含めて最大${MAX_ADDITIONAL_IMAGES}枚です。`, 'error');
                    return;
                }
                const mimeTypes = config.allowedMimeTypes.join(',');
                const view = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS)
                    .setIncludeFolders(false)
                    .setSelectFolderEnabled(false)
                    .setMode(window.google.picker.DocsViewMode.LIST);
                const builder = new window.google.picker.PickerBuilder()
                    .addView(view)
                    .setAppId(String(config.appId))
                    .setOAuthToken(accessToken)
                    .setDeveloperKey(config.apiKey)
                    .setOrigin(window.location.origin)
                    .setSelectableMimeTypes(mimeTypes)
                    .setMaxItems(remaining)
                    .setLocale('ja')
                    .setTitle(target === 'main' ? 'メイン画像を1枚選択' : '追加画像を選択')
                    .setCallback((data) => {
                        if (data.action === window.google.picker.Action.CANCEL) {
                            setStatus('Google Driveでの選択をキャンセルしました。', 'info');
                            return;
                        }
                        if (data.action === window.google.picker.Action.PICKED) {
                            void handlePicked(target, data.docs || []).catch((error) => {
                                setStatus(error.message || 'Google Drive画像を取得できませんでした。', 'error');
                            });
                        }
                    });
                if (target === 'additional') {
                    builder.enableFeature(window.google.picker.Feature.MULTISELECT_ENABLED);
                }
                builder.build().setVisible(true);
                setStatus('Google Driveから画像を選択してください。', 'info');
            } catch (error) {
                setStatus(error.message || 'Google Driveを開けませんでした。', 'error');
            }
        }

        if (mainDriveButton) {
            mainDriveButton.replaceChildren(buildDriveIcon(), document.createTextNode('Google Driveから選択'));
            mainDriveButton.addEventListener('click', () => void openPicker('main'));
        }
        if (additionalDriveButton) {
            additionalDriveButton.replaceChildren(buildDriveIcon(), document.createTextNode('Google Driveから選択'));
            additionalDriveButton.addEventListener('click', () => void openPicker('additional'));
        }

        async function initializeConfig() {
            try {
                const response = await fetch(CONFIG_URL, { credentials: 'same-origin' });
                if (!response.ok) throw new Error('Google Drive設定を確認できませんでした。');
                config = await response.json();
                if (!config.enabled) {
                    setStatus(config.message || 'Google Drive連携は未設定です。PCからの選択は利用できます。', 'info');
                    return;
                }
                if (!config.apiKey || !config.clientId || !config.appId || !Array.isArray(config.allowedMimeTypes)) {
                    throw new Error('Google Drive設定が不足しています。PCからの選択は利用できます。');
                }
                if (mainDriveButton) mainDriveButton.disabled = false;
                if (additionalDriveButton) additionalDriveButton.disabled = false;
                setStatus('PC / 端末またはGoogle Driveから画像を選択できます。', 'success');
            } catch (error) {
                setStatus(error.message || 'Google Drive設定を確認できませんでした。PCからの選択は利用できます。', 'error');
            }
        }

        async function prepareFormData(resizeImage) {
            const formData = new FormData(form);
            formData.delete('photo');
            formData.delete('additional_photos');
            formData.delete('google_drive_photo_file');
            formData.delete('google_drive_photo_receipt');
            formData.delete('google_drive_additional_files');
            formData.delete('google_drive_additional_receipts');

            if (mainEntry) {
                if (mainEntry.source === 'drive') {
                    formData.append('google_drive_photo_file', mainEntry.file, mainEntry.file.name);
                    formData.append('google_drive_photo_receipt', mainEntry.receipt);
                } else {
                    const file = typeof resizeImage === 'function'
                        ? await resizeImage(mainEntry.file)
                        : mainEntry.file;
                    formData.append('photo', file, file.name);
                }
            }

            for (const entry of localAdditionalEntries) {
                const file = typeof resizeImage === 'function'
                    ? await resizeImage(entry.file)
                    : entry.file;
                formData.append('additional_photos', file, file.name);
            }
            for (const entry of driveAdditionalEntries) {
                formData.append('google_drive_additional_files', entry.file, entry.file.name);
                formData.append('google_drive_additional_receipts', entry.receipt);
            }
            return formData;
        }

        updateCount();
        void initializeConfig();

        return {
            prepareFormData,
            getState: () => ({
                mainSource: mainEntry ? mainEntry.source : null,
                localAdditionalCount: localAdditionalEntries.length,
                driveAdditionalCount: driveAdditionalEntries.length,
                totalAdditionalCount: selectedAdditionalCount(),
            }),
        };
    }

    window.KaikaProductImagePicker = { init };
})();
