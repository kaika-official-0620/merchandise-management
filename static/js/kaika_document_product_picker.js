(function () {
    'use strict';

    const collator = new Intl.Collator('ja', {
        numeric: true,
        sensitivity: 'base'
    });

    function normalizeText(value) {
        let text = String(value ?? '');
        if (typeof text.normalize === 'function') {
            text = text.normalize('NFKC');
        }
        return text.toLocaleLowerCase('ja-JP').replace(/\s+/g, ' ').trim();
    }

    function resolvePhotoUrl(path) {
        const value = String(path || '').replace(/\\/g, '/').trim();
        if (!value) return '';
        if (/^https?:\/\//i.test(value)) return value;
        const normalized = value.replace(/^\/+/, '').replace(/^static\//, '');
        return `/static/${normalized}`;
    }

    function compareText(left, right) {
        return collator.compare(normalizeText(left), normalizeText(right));
    }

    function timestamp(value) {
        const parsed = Date.parse(value || '');
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function productStatus(product) {
        if (product.current_status_label) return String(product.current_status_label);
        if (product.sale_date) return '売却済み';
        if (product.is_shipped) return '発送済み';
        if (product.appraisal_status === 'waiting') return '査定待ち';
        if (product.appraisal_status === 'inspecting') return '査定中';
        if (product.is_listed) return '出品中';
        return '未出品';
    }

    function appendThumbnail(container, product, className) {
        const photoUrl = resolvePhotoUrl(product.photo_path);
        if (!photoUrl) {
            const placeholder = document.createElement('span');
            placeholder.className = `${className}-placeholder`;
            placeholder.textContent = '画像なし';
            container.appendChild(placeholder);
            return;
        }
        const image = document.createElement('img');
        image.className = className;
        image.src = photoUrl;
        image.alt = `${product.product_name || `商品ID ${product.id}`} の画像`;
        image.loading = 'lazy';
        image.addEventListener('error', () => {
            const placeholder = document.createElement('span');
            placeholder.className = `${className}-placeholder`;
            placeholder.textContent = '画像なし';
            image.replaceWith(placeholder);
        }, { once: true });
        container.appendChild(image);
    }

    class KaikaDocumentProductPicker {
        constructor(root, options = {}) {
            this.root = root;
            this.input = document.getElementById(root.dataset.inputId || '');
            this.search = root.querySelector('[data-kaika-picker-search]');
            this.sort = root.querySelector('[data-kaika-picker-sort]');
            this.clearSearch = root.querySelector('[data-kaika-picker-clear-search]');
            this.clearSelection = root.querySelector('[data-kaika-picker-clear-selection]');
            this.results = root.querySelector('[data-kaika-picker-results]');
            this.empty = root.querySelector('[data-kaika-picker-empty]');
            this.emptyDefaultText = this.empty?.textContent || '該当する開花商品はありません';
            this.count = root.querySelector('[data-kaika-picker-count]');
            this.selected = root.querySelector('[data-kaika-picker-selected]');
            this.selectedMedia = root.querySelector('[data-kaika-picker-selected-media]');
            this.selectedName = root.querySelector('[data-kaika-picker-selected-name]');
            this.selectedCode = root.querySelector('[data-kaika-picker-selected-code]');
            this.products = [];
            this.selectedId = String(this.input?.value || options.selectedId || '');
            this.renderScheduled = false;

            this.search?.addEventListener('input', () => this.scheduleRender());
            this.sort?.addEventListener('change', () => this.render());
            this.clearSearch?.addEventListener('click', () => {
                if (this.search) this.search.value = '';
                this.render();
                this.search?.focus();
            });
            this.clearSelection?.addEventListener('click', () => this.select(''));
            this.renderSelected();
        }

        scheduleRender() {
            if (this.renderScheduled) return;
            this.renderScheduled = true;
            window.requestAnimationFrame(() => {
                this.renderScheduled = false;
                this.render();
            });
        }

        setProducts(products) {
            if (this.empty) this.empty.textContent = this.emptyDefaultText;
            this.products = (Array.isArray(products) ? products : []).map((product, index) => ({
                ...product,
                id: Number(product.id),
                _sourceIndex: index,
                _searchText: normalizeText([
                    product.product_name,
                    product.brand_name,
                    product.kaika_product_code,
                    product.id,
                    product.model_number
                ].filter(Boolean).join(' '))
            })).filter((product) => Number.isInteger(product.id) && product.id > 0);
            if (this.selectedId && !this.products.some((product) => String(product.id) === this.selectedId)) {
                this.selectedId = '';
                if (this.input) this.input.value = '';
            }
            this.render();
            this.renderSelected();
        }

        setLoadError(message) {
            this.setProducts([]);
            if (this.empty) {
                this.empty.textContent = message || '商品一覧の取得に失敗しました。ページを再読み込みしてください。';
                this.empty.hidden = false;
            }
        }

        getSelected() {
            return this.products.find((product) => String(product.id) === this.selectedId) || null;
        }

        getVisibleProducts() {
            const tokens = normalizeText(this.search?.value).split(' ').filter(Boolean);
            const filtered = tokens.length
                ? this.products.filter((product) => tokens.every((token) => product._searchText.includes(token)))
                : [...this.products];
            const mode = this.sort?.value || 'newest';
            const compareId = (left, right) => left.id - right.id;
            filtered.sort((left, right) => {
                let result = 0;
                if (mode === 'oldest') {
                    result = timestamp(left.created_at) - timestamp(right.created_at);
                    return result || compareId(left, right);
                }
                else if (mode === 'name_asc') result = compareText(left.product_name, right.product_name);
                else if (mode === 'name_desc') result = compareText(right.product_name, left.product_name);
                else if (mode === 'brand_asc') result = compareText(left.brand_name, right.brand_name);
                else if (mode === 'code_asc') result = compareText(left.kaika_product_code, right.kaika_product_code);
                else if (mode === 'code_desc') result = compareText(right.kaika_product_code, left.kaika_product_code);
                else {
                    result = timestamp(right.created_at) - timestamp(left.created_at);
                    return result || compareId(right, left);
                }
                return result || compareId(left, right);
            });
            return filtered;
        }

        select(rawId) {
            const nextId = String(rawId || '');
            this.selectedId = this.products.some((product) => String(product.id) === nextId) ? nextId : '';
            if (this.input) this.input.value = this.selectedId;
            this.render();
            this.renderSelected();
            this.root.dispatchEvent(new CustomEvent('kaika-product-change', {
                bubbles: true,
                detail: { product: this.getSelected() }
            }));
        }

        renderSelected() {
            const product = this.getSelected();
            if (!this.selected) return;
            this.selected.hidden = !product;
            if (!product) {
                if (this.selectedMedia) this.selectedMedia.innerHTML = '';
                if (this.selectedName) this.selectedName.textContent = '';
                if (this.selectedCode) this.selectedCode.textContent = '';
                return;
            }
            if (this.selectedMedia) {
                this.selectedMedia.innerHTML = '';
                appendThumbnail(this.selectedMedia, product, 'kaika-document-picker__thumb');
            }
            if (this.selectedName) this.selectedName.textContent = product.product_name || `商品ID ${product.id}`;
            if (this.selectedCode) {
                const code = product.kaika_product_code || '管理番号未設定';
                this.selectedCode.textContent = `開花管理番号: ${code} / 商品ID: ${product.id}`;
            }
        }

        render() {
            if (!this.results) return;
            const visibleProducts = this.getVisibleProducts();
            this.results.innerHTML = '';
            visibleProducts.forEach((product) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'kaika-document-picker__item';
                button.dataset.productId = String(product.id);
                button.setAttribute('aria-pressed', String(String(product.id) === this.selectedId));
                if (String(product.id) === this.selectedId) button.classList.add('is-selected');
                appendThumbnail(button, product, 'kaika-document-picker__thumb');

                const body = document.createElement('span');
                body.className = 'kaika-document-picker__item-body';
                const name = document.createElement('span');
                name.className = 'kaika-document-picker__name';
                name.textContent = product.product_name || `商品ID ${product.id}`;
                const code = document.createElement('span');
                code.className = 'kaika-document-picker__code';
                code.textContent = `開花管理番号: ${product.kaika_product_code || '-'} / ID: ${product.id}`;
                const meta = document.createElement('span');
                meta.className = 'kaika-document-picker__meta';
                meta.textContent = [
                    `ブランド: ${product.brand_name || '-'}`,
                    `型番: ${product.model_number || '-'}`
                ].join(' / ');
                const status = document.createElement('span');
                status.className = 'kaika-document-picker__status';
                status.textContent = productStatus(product);
                body.append(name, code, meta, status);
                button.appendChild(body);
                button.addEventListener('click', () => this.select(product.id));
                this.results.appendChild(button);
            });
            if (this.count) this.count.textContent = `表示 ${visibleProducts.length}件 / 全${this.products.length}件`;
            if (this.empty) this.empty.hidden = visibleProducts.length !== 0;
        }
    }

    window.KaikaDocumentProductPicker = {
        mount(rootOrSelector, options) {
            const root = typeof rootOrSelector === 'string'
                ? document.querySelector(rootOrSelector)
                : rootOrSelector;
            if (!root) throw new Error('開花商品選択UIの初期化先が見つかりません。');
            return new KaikaDocumentProductPicker(root, options);
        },
        normalizeText
    };
}());
