function togglePanel(id) {
    const panel = document.getElementById(id);
    if (!panel) return;
    panel.hidden = !panel.hidden;
}

function enableEdgeScroll() {
    document.querySelectorAll('.table-wrap').forEach((wrap) => {
        wrap.classList.add('edge-scroll');
        wrap.addEventListener('mousemove', (event) => {
            const rect = wrap.getBoundingClientRect();
            const x = event.clientX - rect.left;
            const edge = 64;
            if (x < edge) {
                wrap.scrollLeft -= 18;
            } else if (x > rect.width - edge) {
                wrap.scrollLeft += 18;
            }
        });
    });
}

document.addEventListener('DOMContentLoaded', enableEdgeScroll);
