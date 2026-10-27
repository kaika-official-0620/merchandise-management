'use strict';
const assert = require('node:assert/strict');
const { compute, ratio } = require('../static/js/profit-calculator.js');
const base = {mode:'profit', feeMode:'amount', sale:'30000', purchase:'20000', fee:'3000', kaika:'1155', shipping:'800', materials:'200', monthly:'0', adjustment:'0'};
let count = 0;
function test(name, fn) { fn(); count++; console.log('PASS ' + name); }
test('all yen costs and signed returns are reflected once', () => {
    const out = compute(base);
    assert.equal(out.net,4845n); assert.equal(out.margin,'16.2%'); assert.equal(out.returnRate,'24.2%');
    const adjusted = compute({...base, monthly:'3278', adjustment:'-500'});
    assert.equal(adjusted.net,1067n); assert.equal(adjusted.product,4345n);
});
test('zero fees remain zero and are not replaced by legacy defaults', () => {
    assert.equal(compute({...base, fee:'0',kaika:'0',shipping:'0',materials:'0'}).net,10000n);
});
test('blank and malformed amounts never become free', () => {
    for (const key of ['sale','purchase','fee','kaika','shipping','materials','monthly','adjustment']) assert.throws(() => compute({...base,[key]:''}));
    for (const value of ['1.5','NaN','Infinity','1e3','-1','9007199254740993','1000000000001']) assert.throws(() => compute({...base,purchase:value}));
});
test('zero denominators are unavailable, losses keep their sign', () => {
    assert.equal(compute({...base,sale:'0'}).margin,null);
    assert.equal(compute({...base,purchase:'0'}).returnRate,null);
    assert.equal(ratio(-1n,3n),'-33.3%');
});
test('rate fee uses exact integer arithmetic at monetary boundaries', () => {
    for (const sale of ['19999','20000','49999','50000','99999','100000','199999','200000']) {
        const out=compute({...base,sale,feeMode:'rate',feeRate:'12.34'});
        assert.equal(out.fee,BigInt(sale)*1234n/10000n);
    }
    assert.equal(compute({...base,feeMode:'rate',feeRate:'0'}).fee,0n);
    for (const value of ['','100','-1','0.001','NaN']) assert.throws(()=>compute({...base,feeMode:'rate',feeRate:value}));
});
test('inverse returns the smallest integer sale meeting the target', () => {
    for (const feeRate of ['0','10','12.34','99.99']) {
        const input={...base, mode:'target', target:'5000',feeMode:'rate',feeRate};
        const out=compute(input);
        assert.ok(out.net>=5000n);
        assert.ok(compute({...input,mode:'profit',sale:String(out.sale-1n)}).net<5000n);
    }
    const fixed=compute({...base,mode:'target',target:'0'});
    assert.equal(fixed.sale,25155n); assert.equal(fixed.net,0n);
});
test('ROI target rounds up to avoid falling below the chosen return', () => {
    const input={...base,mode:'return',purchase:'101',returnRate:'5.25'};
    const out=compute(input);
    assert.equal(out.net,6n);
    assert.throws(()=>compute({...input,purchase:'0'}));
});
test('unreachable target is an error rather than a false zero sale', () => {
    assert.throws(()=>compute({...base,mode:'target',target:'1000000000000',feeMode:'rate',feeRate:'99.99'}));
});
console.log(JSON.stringify({passed:count,scope:'pure calculator, no data writes or billing'}));
