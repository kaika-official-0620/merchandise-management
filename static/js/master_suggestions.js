(function () {
    function normalizeKeyword(input) {
        return String(input || '')
            .toLowerCase()
            .normalize('NFKC')
            .replace(/\s+/g, '')
            .trim();
    }

    function splitKeywords(value) {
        if (Array.isArray(value)) {
            return value
                .map(function (item) { return String(item || '').trim(); })
                .filter(Boolean);
        }

        return String(value || '')
            .split(/[\r\n,\/]+/)
            .map(function (item) { return item.trim(); })
            .filter(Boolean);
    }

    function parseDateValue(value) {
        var timestamp = Date.parse(value || '');
        return Number.isFinite(timestamp) ? timestamp : 0;
    }

    function scoreLocalTokens(query, primaryTokens, aliasTokens) {
        if (!query) {
            return 0;
        }

        var bestScore = -1;

        function inspectToken(token, exactScore, prefixScore, partialScore) {
            var normalized = normalizeKeyword(token);
            if (!normalized) {
                return;
            }
            if (normalized === query) {
                bestScore = Math.max(bestScore, exactScore);
                return;
            }
            if (normalized.indexOf(query) === 0) {
                bestScore = Math.max(bestScore, prefixScore);
                return;
            }
            if (normalized.indexOf(query) !== -1) {
                bestScore = Math.max(bestScore, partialScore);
            }
        }

        (primaryTokens || []).forEach(function (token) {
            inspectToken(token, 1000, 800, 600);
        });
        (aliasTokens || []).forEach(function (token) {
            inspectToken(token, 500, 400, 300);
        });

        return bestScore;
    }

    function sortCandidates(a, b) {
        if ((b.score || 0) !== (a.score || 0)) {
            return (b.score || 0) - (a.score || 0);
        }
        if ((b.displayPriority || 0) !== (a.displayPriority || 0)) {
            return (b.displayPriority || 0) - (a.displayPriority || 0);
        }
        if ((b.useCount || 0) !== (a.useCount || 0)) {
            return (b.useCount || 0) - (a.useCount || 0);
        }
        return (b.lastUsedAt || 0) - (a.lastUsedAt || 0);
    }

    function clearNode(node) {
        while (node.firstChild) {
            node.removeChild(node.firstChild);
        }
    }

    function renderEmpty(dropdown, message) {
        clearNode(dropdown);
        if (!message) {
            dropdown.classList.remove('open');
            return;
        }

        var emptyNode = document.createElement('div');
        emptyNode.className = 'dropdown-no-match';
        emptyNode.textContent = message;
        dropdown.appendChild(emptyNode);
        dropdown.classList.add('open');
    }

    function createMetaNode(text) {
        if (!text) {
            return null;
        }
        var metaNode = document.createElement('div');
        metaNode.className = 'dropdown-meta';
        metaNode.textContent = text;
        return metaNode;
    }

    function buildProductSearchTokens(product) {
        return {
            primary: [product.product_name || ''].filter(Boolean),
            alias: splitKeywords(product.aliases_list || product.aliases || '')
        };
    }

    function buildBrandSearchTokens(brand, categoryName) {
        return {
            primary: [
                brand.display_name || '',
                brand.value || '',
                categoryName || ''
            ].filter(Boolean),
            alias: splitKeywords(brand.aliases || '').concat(splitKeywords(brand.keywords || ''))
        };
    }

    function buildCategorySearchTokens(category) {
        return {
            primary: [category.name || ''].filter(Boolean),
            alias: splitKeywords(category.aliases || '')
        };
    }

    function dedupeTerms(items) {
        var seen = {};
        return (items || []).filter(function (item) {
            var normalized = normalizeKeyword(item);
            if (!normalized || seen[normalized]) {
                return false;
            }
            seen[normalized] = true;
            return true;
        });
    }

    function hasJapaneseChars(value) {
        return /[ぁ-んァ-ヶ一-龠々ー]/.test(String(value || ''));
    }

    function resolvePreferredBrandLabel(brand) {
        var aliases = splitKeywords(brand.aliases || '');
        var preferredJapaneseAlias = aliases.find(function (token) {
            var normalized = normalizeKeyword(token);
            if (!normalized || !hasJapaneseChars(token)) {
                return false;
            }
            return normalized !== normalizeKeyword(brand.value || '') &&
                normalized !== normalizeKeyword(brand.display_name || '');
        });

        return preferredJapaneseAlias || brand.display_name || brand.value || '';
    }

    function buildMasterProductCandidates(brand) {
        var brandLabel = resolvePreferredBrandLabel(brand);
        var aliasTerms = dedupeTerms(splitKeywords(brand.aliases || ''));
        var keywordTerms = dedupeTerms(splitKeywords(brand.keywords || ''));
        var brandIdentityTerms = dedupeTerms([brand.value, brand.display_name, brandLabel]);
        var normalizedBrandIdentity = brandIdentityTerms.map(function (term) {
            return normalizeKeyword(term);
        });
        var categoryNormalized = normalizeKeyword(brand.category_name || '');
        var searchableTerms = dedupeTerms(aliasTerms.concat(keywordTerms));
        var identityTerms = dedupeTerms(brandIdentityTerms.concat(aliasTerms.filter(function (term) {
            var normalized = normalizeKeyword(term);
            if (!normalized) {
                return false;
            }
            if (/^[a-z0-9&.'-]{1,4}$/i.test(String(term || '').trim())) {
                return true;
            }
            return normalizedBrandIdentity.some(function (brandTerm) {
                return normalized === brandTerm ||
                    normalized.indexOf(brandTerm) !== -1 ||
                    brandTerm.indexOf(normalized) !== -1;
            });
        })));
        var normalizedIdentity = identityTerms.map(function (term) {
            return normalizeKeyword(term);
        });

        var lineTerms = searchableTerms.filter(function (term) {
            var normalized = normalizeKeyword(term);
            if (!normalized) {
                return false;
            }
            if (normalized === categoryNormalized) {
                return false;
            }
            if (normalizedIdentity.indexOf(normalized) !== -1) {
                return false;
            }
            return !normalizedBrandIdentity.some(function (brandTerm) {
                return normalized.indexOf(brandTerm) !== -1 ||
                    brandTerm.indexOf(normalized) !== -1;
            });
        });

        var candidates = [{
            suggestion_kind: 'master_brand',
            title: brandLabel,
            insert_text: brandLabel,
            meta_text: [brand.category_name || '', 'ブランド候補'].filter(Boolean).join(' | '),
            displayPriority: 40,
            brand: brand,
            useCount: brand.useCount || 0,
            lastUsedAt: brand.lastUsedAt || 0,
            searchTokens: {
                primary: [brandLabel, brand.display_name || '', brand.value || ''].filter(Boolean),
                alias: dedupeTerms(aliasTerms.concat(keywordTerms))
            }
        }];

        lineTerms.slice(0, 20).forEach(function (term) {
            candidates.push({
                suggestion_kind: 'master_keyword',
                title: brandLabel + ' ' + term,
                insert_text: brandLabel + ' ' + term,
                meta_text: [
                    brand.category_name || '',
                    'ブランド: ' + (brandLabel || brand.value || ''),
                    '名称候補: ' + term
                ].filter(Boolean).join(' | '),
                displayPriority: 30,
                brand: brand,
                useCount: brand.useCount || 0,
                lastUsedAt: brand.lastUsedAt || 0,
                searchTokens: {
                    primary: [brandLabel + ' ' + term, term, brandLabel].filter(Boolean),
                    alias: dedupeTerms([brand.value || '', brand.display_name || ''].concat(aliasTerms).concat(keywordTerms))
                }
            });
        });

        return candidates;
    }

    function scoreFreeTextAgainstTokens(inputValue, primaryTokens, aliasTokens) {
        var normalizedInput = normalizeKeyword(inputValue);
        if (!normalizedInput) {
            return 0;
        }

        var bestScore = -1;

        function inspectToken(token, containsScore, prefixScore, partialScore) {
            var normalized = normalizeKeyword(token);
            if (!normalized) {
                return;
            }
            if (normalizedInput === normalized) {
                bestScore = Math.max(bestScore, containsScore + 200);
                return;
            }
            if (normalizedInput.indexOf(normalized) !== -1) {
                bestScore = Math.max(bestScore, containsScore);
                return;
            }
            if (normalized.indexOf(normalizedInput) === 0) {
                bestScore = Math.max(bestScore, prefixScore);
                return;
            }
            if (normalized.indexOf(normalizedInput) !== -1) {
                bestScore = Math.max(bestScore, partialScore);
            }
        }

        (primaryTokens || []).forEach(function (token) {
            inspectToken(token, 950, 620, 420);
        });
        (aliasTokens || []).forEach(function (token) {
            inspectToken(token, 760, 520, 360);
        });

        return bestScore;
    }

    function MasterSuggestionController(options) {
        this.scope = options.scope || 'admin';
        this.data = options.data || {};
        this.form = document.querySelector(options.formSelector || '#item-form');

        this.productInput = document.getElementById('product_name');
        this.productDropdown = document.getElementById('product-suggestion-dropdown');
        this.brandInput = document.getElementById('brand_name');
        this.brandToggle = document.getElementById('brand-toggle');
        this.brandDropdown = document.getElementById('brand-dropdown');
        this.brandIdInput = document.getElementById('master_brand_id');
        this.categoryInput = document.getElementById('brand_category_name');
        this.categoryToggle = document.getElementById('category-toggle');
        this.categoryDropdown = document.getElementById('category-dropdown');
        this.categoryIdInput = document.getElementById('master_category_id');

        this.products = (this.data.products || []).map(function (product) {
            var searchTokens = buildProductSearchTokens(product);
            return Object.assign({}, product, {
                useCount: Number(product.use_count || 0),
                lastUsedAt: parseDateValue(product.last_used_at),
                searchTokens: searchTokens
            });
        });

        this.categories = (this.data.brand_categories || []).map(function (category) {
            return Object.assign({}, category, {
                useCount: Number(category.use_count || 0),
                lastUsedAt: parseDateValue(category.last_used_at),
                searchTokens: buildCategorySearchTokens(category)
            });
        });

        this.categoryMap = {};
        this.categories.forEach(function (category) {
            this.categoryMap[String(category.id)] = category.name || '';
        }, this);

        this.brands = (this.data.brands || []).map(function (brand) {
            var categoryName = this.categoryMap[String(brand.category_id)] || '';
            return Object.assign({}, brand, {
                category_name: categoryName,
                useCount: Number(brand.use_count || 0),
                lastUsedAt: parseDateValue(brand.last_used_at),
                searchTokens: buildBrandSearchTokens(brand, categoryName)
            });
        }, this);

        this.masterProductSuggestions = [];
        this.brands.forEach(function (brand) {
            this.masterProductSuggestions = this.masterProductSuggestions.concat(buildMasterProductCandidates(brand));
        }, this);
    }

    MasterSuggestionController.prototype.init = function () {
        if (!this.form || !this.productInput || !this.brandInput || !this.categoryInput) {
            return;
        }
        if (this.form.dataset.masterSuggestionReady === '1') {
            return;
        }
        this.form.dataset.masterSuggestionReady = '1';

        this.bindProductSuggestions();
        this.bindRemoteSuggestions(
            this.brandInput,
            this.brandDropdown,
            this.brandToggle,
            'brand',
            this.handleBrandSelection.bind(this)
        );
        this.bindRemoteSuggestions(
            this.categoryInput,
            this.categoryDropdown,
            this.categoryToggle,
            'category',
            this.handleCategorySelection.bind(this)
        );
        this.bindBrandAutoDetect();
        this.bindFormNormalization();
        this.bootstrapExistingSelections();

        document.addEventListener('click', this.handleDocumentClick.bind(this));
    };

    MasterSuggestionController.prototype.handleDocumentClick = function (event) {
        if (!event.target.closest('.brand-combobox')) {
            if (this.productDropdown) {
                this.productDropdown.classList.remove('open');
            }
            if (this.brandDropdown) {
                this.brandDropdown.classList.remove('open');
            }
            if (this.categoryDropdown) {
                this.categoryDropdown.classList.remove('open');
            }
        }
    };

    MasterSuggestionController.prototype.bindProductSuggestions = function () {
        var self = this;
        if (!this.productInput || !this.productDropdown) {
            return;
        }

        function renderCurrentProducts() {
            self.renderProductSuggestions(self.productInput.value);
        }

        this.productInput.addEventListener('input', function () {
            renderCurrentProducts();
        });

        this.productInput.addEventListener('focus', function () {
            renderCurrentProducts();
        });
    };

    MasterSuggestionController.prototype.renderProductSuggestions = function (queryValue) {
        var self = this;
        if (!this.productDropdown) {
            return;
        }

        var normalizedQuery = normalizeKeyword(queryValue);
        var masterCandidates = this.masterProductSuggestions
            .map(function (product) {
                return Object.assign({}, product, {
                    score: normalizedQuery
                        ? scoreFreeTextAgainstTokens(
                            queryValue,
                            product.searchTokens.primary,
                            product.searchTokens.alias
                        )
                        : 0
                });
            })
            .filter(function (product) {
                if (!normalizedQuery) {
                    return product.suggestion_kind === 'master_brand';
                }
                return product.score >= 0;
            })
            .sort(sortCandidates)
            .slice(0, normalizedQuery ? 10 : 8);

        var historyCandidates = [];
        if (normalizedQuery) {
            historyCandidates = this.products
                .map(function (product) {
                    return Object.assign({}, product, {
                        suggestion_kind: 'history',
                        title: product.product_name || '',
                        insert_text: product.product_name || '',
                        meta_text: product.brand_name || '',
                        displayPriority: 10,
                        score: scoreLocalTokens(
                            normalizedQuery,
                            product.searchTokens.primary,
                            product.searchTokens.alias
                        )
                    });
                })
                .filter(function (product) {
                    return product.score >= 0;
                })
                .sort(sortCandidates)
                .slice(0, 6);
        }

        var seenProductTexts = {};
        var candidates = masterCandidates.concat(historyCandidates).filter(function (candidate) {
            var normalized = normalizeKeyword(candidate.insert_text || candidate.title || '');
            if (!normalized || seenProductTexts[normalized]) {
                return false;
            }
            seenProductTexts[normalized] = true;
            return true;
        }).slice(0, 12);

        if (!candidates.length) {
            renderEmpty(this.productDropdown, normalizedQuery ? '候補が見つかりません' : 'ブランド名や名称から候補を表示します');
            return;
        }

        clearNode(this.productDropdown);
        candidates.forEach(function (product) {
            var itemNode = document.createElement('div');
            itemNode.className = 'dropdown-item';
            itemNode.tabIndex = 0;

            var titleNode = document.createElement('div');
            titleNode.textContent = product.title || product.product_name || '';
            itemNode.appendChild(titleNode);

            var metaNode = createMetaNode(product.meta_text || product.brand_name || '');
            if (metaNode) {
                itemNode.appendChild(metaNode);
            }

            itemNode.addEventListener('click', function () {
                self.applyProductSuggestion(product);
                self.productDropdown.classList.remove('open');
            });

            this.productDropdown.appendChild(itemNode);
        }, this);

        this.productDropdown.classList.add('open');
    };

    MasterSuggestionController.prototype.applyProductSuggestion = function (item) {
        this.productInput.value = item.insert_text || item.title || item.product_name || '';

        if (item.brand) {
            this.handleBrandSelection(item.brand);
            return;
        }

        if (item.brand_name) {
            this.applyClosestBrandMatch(item.brand_name, 400);
        }
    };

    MasterSuggestionController.prototype.bindRemoteSuggestions = function (input, dropdown, toggle, type, onSelect) {
        var self = this;
        if (!input || !dropdown) {
            return;
        }

        var timer = null;

        function requestSuggestions() {
            if (timer) {
                clearTimeout(timer);
            }
            timer = window.setTimeout(function () {
                self.loadRemoteSuggestions(type, input.value, dropdown, onSelect);
            }, 120);
        }

        input.addEventListener('input', function () {
            if (type === 'brand' && self.brandIdInput) {
                self.brandIdInput.value = '';
            }
            if (type === 'category' && self.categoryIdInput) {
                self.categoryIdInput.value = '';
            }
            requestSuggestions();
        });

        input.addEventListener('focus', function () {
            requestSuggestions();
        });

        input.addEventListener('blur', function () {
            window.setTimeout(function () {
                if (type === 'brand') {
                    self.applyClosestBrandMatch(input.value, 500);
                } else if (type === 'category') {
                    self.applyClosestCategoryMatch(input.value, 500);
                }
            }, 140);
        });

        if (toggle) {
            toggle.addEventListener('click', function (event) {
                event.preventDefault();
                event.stopPropagation();
                if (dropdown.classList.contains('open')) {
                    dropdown.classList.remove('open');
                    return;
                }
                self.loadRemoteSuggestions(type, input.value, dropdown, onSelect);
                input.focus();
            });
        }
    };

    MasterSuggestionController.prototype.getBrandCandidates = function (queryValue) {
        var normalizedQuery = normalizeKeyword(queryValue);
        var activeCategoryId = this.categoryIdInput && this.categoryIdInput.value
            ? String(this.categoryIdInput.value)
            : '';
        var activeCategoryName = this.categoryInput && this.categoryInput.value
            ? normalizeKeyword(this.categoryInput.value)
            : '';

        return this.brands
            .filter(function (brand) {
                if (activeCategoryId && String(brand.category_id || '') !== activeCategoryId) {
                    return false;
                }
                if (activeCategoryName && !activeCategoryId) {
                    var brandCategoryName = normalizeKeyword(brand.category_name || '');
                    if (brandCategoryName && brandCategoryName.indexOf(activeCategoryName) === -1) {
                        return false;
                    }
                }
                return true;
            })
            .map(function (brand) {
                return Object.assign({}, brand, {
                    score: scoreLocalTokens(
                        normalizedQuery,
                        brand.searchTokens.primary,
                        brand.searchTokens.alias
                    )
                });
            })
            .filter(function (brand) {
                return !normalizedQuery || brand.score >= 0;
            })
            .sort(sortCandidates)
            .slice(0, 12);
    };

    MasterSuggestionController.prototype.getCategoryCandidates = function (queryValue) {
        var normalizedQuery = normalizeKeyword(queryValue);
        var selectedCategoryId = this.categoryIdInput && this.categoryIdInput.value
            ? String(this.categoryIdInput.value)
            : '';
        var hintedBrand = this.findBestBrandMatch(this.brandInput ? this.brandInput.value : '', 400);
        var hintedCategoryId = hintedBrand && hintedBrand.category_id
            ? String(hintedBrand.category_id)
            : '';

        return this.categories
            .map(function (category) {
                var score = scoreLocalTokens(
                    normalizedQuery,
                    category.searchTokens.primary,
                    category.searchTokens.alias
                );
                if (!normalizedQuery) {
                    score = 0;
                }
                if (selectedCategoryId && String(category.id) === selectedCategoryId) {
                    score += 250;
                } else if (hintedCategoryId && String(category.id) === hintedCategoryId) {
                    score += 150;
                }
                return Object.assign({}, category, {
                    score: score
                });
            })
            .filter(function (category) {
                return !normalizedQuery || category.score >= 0;
            })
            .sort(sortCandidates)
            .slice(0, 12);
    };

    MasterSuggestionController.prototype.loadRemoteSuggestions = function (type, queryValue, dropdown, onSelect) {
        try {
            var items = type === 'brand'
                ? this.getBrandCandidates(queryValue)
                : this.getCategoryCandidates(queryValue);
            this.renderRemoteSuggestions(dropdown, items, onSelect, type);
        } catch (error) {
            console.error('Master suggestion load error:', error);
            renderEmpty(dropdown, '候補の読込に失敗しました');
        }
    };

    MasterSuggestionController.prototype.renderRemoteSuggestions = function (dropdown, items, onSelect, type) {
        var self = this;
        if (!dropdown) {
            return;
        }

        if (!items.length) {
            renderEmpty(dropdown, '候補が見つかりません');
            return;
        }

        clearNode(dropdown);
        items.forEach(function (item) {
            var itemNode = document.createElement('div');
            itemNode.className = 'dropdown-item';
            itemNode.tabIndex = 0;

            var titleNode = document.createElement('div');
            titleNode.textContent = type === 'brand'
                ? (item.display_name || item.value || '')
                : (item.name || '');
            itemNode.appendChild(titleNode);

            var metaText = '';
            if (type === 'brand') {
                var brandMeta = [];
                if (item.category_name) {
                    brandMeta.push(item.category_name);
                }
                var aliasText = splitKeywords(item.aliases).filter(function (token) {
                    return normalizeKeyword(token) !== normalizeKeyword(item.value || '');
                }).slice(0, 3).join(', ');
                var keywordText = splitKeywords(item.keywords).slice(0, 3).join(', ');
                if (aliasText) {
                    brandMeta.push('検索名: ' + aliasText);
                }
                if (keywordText) {
                    brandMeta.push('キーワード: ' + keywordText);
                }
                metaText = brandMeta.join(' | ');
            } else if (item.aliases) {
                metaText = splitKeywords(item.aliases).slice(0, 3).join(', ');
            }

            var metaNode = createMetaNode(metaText);
            if (metaNode) {
                itemNode.appendChild(metaNode);
            }

            itemNode.addEventListener('click', function () {
                onSelect(item);
                dropdown.classList.remove('open');
            });

            dropdown.appendChild(itemNode);
        });

        dropdown.classList.add('open');
    };

    MasterSuggestionController.prototype.findBestBrandMatch = function (queryValue, minScore) {
        var normalizedQuery = normalizeKeyword(queryValue);
        if (!normalizedQuery || normalizedQuery.length < 2) {
            return null;
        }

        var candidates = this.getBrandCandidates(queryValue);
        if (!candidates.length) {
            return null;
        }

        return (candidates[0].score || 0) >= (minScore || 0) ? candidates[0] : null;
    };

    MasterSuggestionController.prototype.findBestCategoryMatch = function (queryValue, minScore) {
        var normalizedQuery = normalizeKeyword(queryValue);
        if (!normalizedQuery || normalizedQuery.length < 2) {
            return null;
        }

        var candidates = this.getCategoryCandidates(queryValue);
        if (!candidates.length) {
            return null;
        }

        return (candidates[0].score || 0) >= (minScore || 0) ? candidates[0] : null;
    };

    MasterSuggestionController.prototype.findBestBrandFromFreeText = function (queryValue) {
        var normalizedQuery = normalizeKeyword(queryValue);
        if (!normalizedQuery || normalizedQuery.length < 2) {
            return null;
        }

        var candidates = this.brands
            .map(function (brand) {
                return Object.assign({}, brand, {
                    score: scoreFreeTextAgainstTokens(
                        queryValue,
                        brand.searchTokens.primary,
                        brand.searchTokens.alias
                    )
                });
            })
            .filter(function (brand) {
                return brand.score >= 0;
            })
            .sort(sortCandidates);

        if (!candidates.length) {
            return null;
        }

        return (candidates[0].score || 0) >= 520 ? candidates[0] : null;
    };

    MasterSuggestionController.prototype.applyClosestBrandMatch = function (queryValue, minScore) {
        var match = this.findBestBrandMatch(queryValue, minScore || 0);
        if (match) {
            this.handleBrandSelection(match);
        }
        return match;
    };

    MasterSuggestionController.prototype.applyClosestCategoryMatch = function (queryValue, minScore) {
        var match = this.findBestCategoryMatch(queryValue, minScore || 0);
        if (match) {
            this.handleCategorySelection(match);
        }
        return match;
    };

    MasterSuggestionController.prototype.handleBrandSelection = function (item) {
        this.brandInput.value = item.display_name || item.value || '';
        this.brandInput.dataset.autoDetected = 'false';
        if (this.brandIdInput) {
            this.brandIdInput.value = item.id || '';
        }

        if (item.category_id && this.categoryIdInput) {
            this.categoryIdInput.value = item.category_id;
        }
        if (item.category_name && this.categoryInput) {
            this.categoryInput.value = item.category_name;
        }
    };

    MasterSuggestionController.prototype.handleCategorySelection = function (item) {
        this.categoryInput.value = item.name || '';
        if (this.categoryIdInput) {
            this.categoryIdInput.value = item.id || '';
        }
    };

    MasterSuggestionController.prototype.bindBrandAutoDetect = function () {
        var self = this;
        if (!this.productInput || !this.brandInput) {
            return;
        }

        this.productInput.addEventListener('input', function () {
            var match = self.findBestBrandFromFreeText(self.productInput.value);
            if (!match) {
                return;
            }
            if (self.brandInput.value && self.brandInput.dataset.autoDetected !== 'true') {
                return;
            }

            self.handleBrandSelection(match);
            self.brandInput.dataset.autoDetected = 'true';
            self.brandInput.classList.add('brand-auto-detected');
            window.setTimeout(function () {
                self.brandInput.classList.remove('brand-auto-detected');
            }, 1000);
        });
    };

    MasterSuggestionController.prototype.bindFormNormalization = function () {
        var self = this;
        if (!this.form) {
            return;
        }

        this.form.addEventListener('submit', function () {
            if (self.brandInput && self.brandInput.value) {
                self.applyClosestBrandMatch(self.brandInput.value, 500);
            }
            if (self.categoryInput && self.categoryInput.value) {
                self.applyClosestCategoryMatch(self.categoryInput.value, 500);
            }
        }, true);
    };

    MasterSuggestionController.prototype.bootstrapExistingSelections = function () {
        if (this.brandInput && this.brandInput.value && this.categoryInput && !this.categoryInput.value) {
            this.applyClosestBrandMatch(this.brandInput.value, 500);
        }
        if (this.categoryInput && this.categoryInput.value) {
            this.applyClosestCategoryMatch(this.categoryInput.value, 500);
        }
    };

    window.MasterSuggestionHelper = {
        init: function (options) {
            var controller = new MasterSuggestionController(options || {});
            controller.init();
            return controller;
        }
    };
})();
