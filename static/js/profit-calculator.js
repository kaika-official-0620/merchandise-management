(function () {
    'use strict';
    // Yen and percentage arithmetic are integer-only. This simulator never writes business data.
    const LIMIT = 1000000000000n;
    function money(value, label, signed) {
        const text = String(value == null ? '' : value).trim();
        if (!(signed ? /^-?\d{1,13}$/ : /^\d{1,13}$/).test(text)) throw new Error(label + 'を1円単位で入力してください。');
        const parsed = BigInt(text);
        if (parsed > LIMIT || parsed < -LIMIT) throw new Error(label + 'が入力範囲を超えています。');
        return parsed;
    }
    function percent(value, label, max) {
        const text = String(value == null ? '' : value).trim();
        if (!/^\d{1,5}(?:\.\d{1,2})?$/.test(text)) throw new Error(label + 'を小数第2位までで入力してください。');
        const parts = text.split('.');
        const parsed = BigInt(parts[0]) * 100n + BigInt((parts[1] || '').padEnd(2, '0'));
        if (parsed > max) throw new Error(label + 'が入力範囲を超えています。');
        return parsed;
    }
    function ratio(numerator, denominator) {
        if (denominator === 0n) return null;
        const negative = numerator < 0n;
        const magnitude = negative ? -numerator : numerator;
        const tenths = (magnitude * 2000n + denominator) / (2n * denominator);
        return (negative && tenths ? '-' : '') + String(tenths / 10n) + '.' + String(tenths % 10n) + '%';
    }
    function compute(input) {
        if (!['profit', 'target', 'return'].includes(input.mode) || !['amount', 'rate'].includes(input.feeMode)) throw new Error('計算方法を選択してください。');
        const purchase = money(input.purchase, '仕入原価');
        const kaika = money(input.kaika, '開花料金');
        const shipping = money(input.shipping, '送料');
        const materials = money(input.materials, '資材・その他の費用');
        const monthly = money(input.monthly, '月額配分');
        const adjustment = money(input.adjustment, '返品等の調整', true);
        const rate = input.feeMode === 'rate' ? percent(input.feeRate, '仮の手数料率', 9999n) : 0n;
        const fixedFee = input.feeMode === 'amount' ? money(input.fee, '販売先手数料') : 0n;
        const feeAt = sale => input.feeMode === 'rate' ? sale * rate / 10000n : fixedFee;
        const cost = purchase + kaika + shipping + materials + monthly - adjustment;
        const netAt = sale => sale - feeAt(sale) - cost;
        let sale;
        if (input.mode === 'profit') {
            sale = money(input.sale, '売上');
        } else {
            if (input.mode === 'return' && purchase === 0n) throw new Error('原価比の逆算には1円以上の仕入原価が必要です。');
            const target = input.mode === 'target' ? money(input.target, '目標の手残り') :
                (purchase * percent(input.returnRate, '目標手残り率', 1000000n) + 9999n) / 10000n;
            if (netAt(LIMIT) < target) throw new Error('この条件では入力上限内の売上を計算できません。費用と手数料率を確認してください。');
            let low = 0n, high = LIMIT;
            while (low < high) {
                const mid = (low + high) / 2n;
                if (netAt(mid) >= target) high = mid; else low = mid + 1n;
            }
            sale = low;
        }
        const net = netAt(sale);
        return { sale, fee: feeAt(sale), net, product: net + monthly, margin: ratio(net, sale), returnRate: ratio(net, purchase) };
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = { compute, ratio };
    if (typeof document === 'undefined') return;
    const modal = document.getElementById('profitCalcModal');
    const form = document.getElementById('profit-estimate-form');
    if (!modal || !form) return;
    const message = document.getElementById('pe-message');
    const results = document.getElementById('pe-results');
    let returnFocus = null, previousOverflow = '';
    function synchronize() {
        const mode = form.elements.mode.value, feeMode = form.elements.feeMode.value;
        modal.querySelectorAll('[data-profit-mode]').forEach(el => { el.hidden = el.dataset.profitMode !== mode; });
        modal.querySelectorAll('[data-profit-fee]').forEach(el => { el.hidden = el.dataset.profitFee !== feeMode; });
        results.hidden = true;
        message.textContent = '条件を変更した場合は、もう一度「この条件で計算する」を押してください。';
    }
    window.openProfitCalculator = function () {
        returnFocus = document.activeElement;
        previousOverflow = document.body.style.overflow;
        modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
        document.getElementById('pe-mode').focus();
    };
    window.closeProfitCalculator = function () {
        if (!modal.classList.contains('open')) return;
        modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = previousOverflow;
        if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    };
    modal.querySelectorAll('[data-profit-close]').forEach(button => button.addEventListener('click', window.closeProfitCalculator));
    document.addEventListener('keydown', event => {
        if (!modal.classList.contains('open')) return;
        if (event.key === 'Escape') { event.preventDefault(); window.closeProfitCalculator(); }
        if (event.key === 'Tab') {
            const focusable = Array.from(modal.querySelectorAll('button,input,select,summary,a[href]')).filter(el => !el.disabled && el.getClientRects().length);
            const first = focusable[0], last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
    });
    form.addEventListener('input', synchronize);
    form.addEventListener('change', synchronize);
    form.addEventListener('reset', () => setTimeout(synchronize, 0));
    form.addEventListener('submit', event => {
        event.preventDefault();
        results.hidden = true;
        try {
            const data = Object.fromEntries(new FormData(form).entries());
            const output = compute(data);
            for (const key of ['sale', 'fee', 'net', 'product']) {
                const el = document.getElementById('pe-result-' + key);
                el.textContent = '¥' + output[key].toLocaleString('ja-JP');
                el.classList.toggle('negative', output[key] < 0n);
            }
            document.getElementById('pe-result-margin').textContent = output.margin || '計算不可（売上0円）';
            document.getElementById('pe-result-return').textContent = output.returnRate || '計算不可（原価0円）';
            document.getElementById('pe-sale-label').textContent = data.mode === 'profit' ? '売上（税込）' : '必要な売上（税込・入力費用を固定）';
            message.textContent = '入力条件による試算です。実際の請求・利益を確定するものではありません。';
            results.hidden = false;
        } catch (error) { message.textContent = error.message; }
    });
}());
