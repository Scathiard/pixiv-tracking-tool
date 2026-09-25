/* Persistent queue feedback; completion never navigates or refreshes the document. */
(() => {
    'use strict';
    const box = document.getElementById('progress-box');
    if (!box) return;
    box.style.display = 'block';
    const bar = document.getElementById('prog-bar');
    const title = document.getElementById('prog-phase');
    const message = document.getElementById('prog-message');
    const percent = document.getElementById('prog-pct');
    const summary = document.getElementById('prog-detail');
    const errors = document.getElementById('prog-errors');
    const details = document.createElement('details');
    const detailTitle = document.createElement('summary');
    detailTitle.textContent = '任务记录';
    const taskList = document.createElement('div');
    const selectedTask = document.createElement('div');
    const historyList = document.createElement('div');
    const historyButton = document.createElement('button');
    historyButton.className = 'btn-sm';
    historyButton.textContent = '翻查历史记录';
    selectedTask.setAttribute('aria-live', 'polite');
    details.append(detailTitle, taskList, historyButton, historyList, selectedTask);
    box.append(details);
    box.setAttribute('aria-label', '本轮任务进度');
    bar.parentElement.setAttribute('role', 'progressbar');
    bar.parentElement.setAttribute('aria-label', '本轮处理进度');
    bar.parentElement.setAttribute('aria-valuemin', '0');
    bar.parentElement.setAttribute('aria-valuemax', '100');
    const outcomes = {succeeded: '处理完成', partial_failure: '部分失败', failed: '失败', empty: '无需处理'};
    const states = {queued: '已排队', planning: '清点中', ready: '已就绪', running: '正在执行', finished: '已结束', interrupted: '已中断，等待恢复'};
    const phases = {waiting: '等待', checking: '检查', inventory: '清点', metadata: '画师信息', bookmarks: '收藏状态', captions: '作品信息与简介', downloading: '下载', converting: '生成预览', verifying: '核验', thumbnails: '缩略图', deleting: '删除'};
    const units = {artists: '位画师', works: '件作品', pages: '页', assets: '项原件', previews: '件预览', files: '个文件', items: '项'};
    let selectedId = null;
    let requestGeneration = 0;
    let lastRound = null;
    let lastState = null;
    let historyCursor = null;
    let displayedTaskState = null;

    async function json(url, options) {
        const response = await fetch(url, {...options, signal: AbortSignal.timeout(30000)});
        const value = await response.json();
        if (!response.ok || value.ok === false) throw new Error(value.error || value.detail || '操作未完成，请重试');
        return value;
    }

    function row(text) {
        const element = document.createElement('p');
        element.textContent = text;
        return element;
    }

    async function showTask(id, offset = 0, append = false) {
        selectedId = id;
        details.open = true;
        const generation = ++requestGeneration;
        try {
            const task = await json('/api/tasks/' + encodeURIComponent(id) + '?problems_only=true&offset=' + offset);
            if (generation !== requestGeneration) return;
            displayedTaskState = task.state;
            if (!append) {
                selectedTask.replaceChildren(row(task.label + ' · ' + (outcomes[task.result.outcome] || states[task.state])));
                Object.values(task.result.phase_snapshots || {}).forEach(phase => {
                    selectedTask.append(row((phases[phase.phase] || phase.phase) + '：' + phase.done + ' / ' + (phase.total === null ? '总量尚未确定' : phase.total) + ' ' + (units[phase.unit] || phase.unit)));
                });
                if (task.result.inventory_complete === false) selectedTask.append(row('清点有未完成的范围，详见失败原因。'));
                if (task.state === 'finished') {
                    const retry = document.createElement('button');
                    retry.className = 'btn-sm'; retry.textContent = '重新执行该范围';
                    retry.addEventListener('click', async () => {
                        try {
                            const submission = await json('/api/tasks/' + id + '/retry', {method:'POST'});
                            showTask(submission.operation_id);
                        } catch (error) { showToast(error.message, 'error'); }
                    });
                    selectedTask.append(retry);
                }
            }
            selectedTask.querySelector('.more-problems')?.remove();
            task.items.forEach(item => {
                if (item.state === 'failed' || item.state === 'not_executed') {
                    selectedTask.append(row((item.result.message || '未完成') + ' · ' + item.key));
                }
            });
            if (task.has_more_items) {
                const more = document.createElement('button');
                more.className = 'btn-sm more-problems'; more.textContent = '查看更多未完成项';
                more.addEventListener('click', () => showTask(id, offset + 100, true));
                selectedTask.append(more);
            }
            if (task.needs_attention && !append) {
                const acknowledge = document.createElement('button');
                acknowledge.className = 'btn-sm';
                acknowledge.textContent = '标为已查看（保留记录）';
                acknowledge.addEventListener('click', async () => {
                    try { await json('/api/tasks/' + id + '/acknowledge', {method: 'POST'}); await showTask(id); }
                    catch (error) { showToast(error.message, 'error'); }
                });
                selectedTask.append(acknowledge);
            }
        } catch (error) {
            if (generation === requestGeneration) selectedTask.replaceChildren(row(error.message));
        }
    }

    historyButton.addEventListener('click', async () => {
        try {
            const data = await json('/api/tasks?limit=20' + (historyCursor ? '&before=' + encodeURIComponent(historyCursor) : ''));
            if (!historyCursor) {
                historyList.replaceChildren(row('已结束的历史任务'));
                taskList.hidden = true;
                const current = document.createElement('button');
                current.className = 'btn-sm'; current.textContent = '返回当前任务';
                current.addEventListener('click', () => {
                    taskList.hidden = false; historyList.replaceChildren(); historyCursor = null;
                    historyButton.textContent = '翻查历史记录'; historyButton.disabled = false;
                });
                historyList.append(current);
            }
            data.items.forEach(operation => {
                const button = document.createElement('button'); button.className = 'btn-sm';
                button.textContent = operation.label + ' · ' + (outcomes[operation.result.outcome] || '已结束');
                button.addEventListener('click', () => showTask(operation.id));
                const paragraph = document.createElement('p'); paragraph.append(button); historyList.append(paragraph);
            });
            if (data.items.length) historyCursor = data.items[data.items.length - 1].id;
            historyButton.textContent = data.has_more ? '查看更早的记录' : '已显示全部历史记录';
            historyButton.disabled = !data.has_more;
        } catch (error) { showToast(error.message, 'error'); }
    });

    function render(data) {
        const current = data.round;
        box.classList.remove('status-stale');
        title.textContent = current ? outcomes[current.outcome] || (current.state === 'planning' ? '正在清点' : '正在处理') : '暂无运行任务';
        if (data.worker_error) title.textContent = '任务已中断，请重启继续';
        const known = current && current.total !== null && current.total > 0;
        const value = known ? Math.min(100, current.settled / current.total * 100) : 0;
        bar.style.width = value + '%';
        if (known) bar.parentElement.setAttribute('aria-valuenow', String(value));
        else bar.parentElement.removeAttribute('aria-valuenow');
        percent.textContent = known ? Math.floor(value) + '%' : '';
        message.textContent = known ? current.settled + ' / ' + current.total + ' 项已有处理结果' : (current && current.state === 'planning' ? '清单尚未确定，正在检查具体内容' : '');
        const counts = current ? current.counts : {};
        summary.textContent = '成功 ' + (counts.succeeded || 0) + ' · 失败 ' + (counts.failed || 0) + ' · 跳过 ' + (counts.skipped || 0) + ' · 未执行 ' + (counts.not_executed || 0) + (data.next_round_count ? ' · 下一轮等待 ' + data.next_round_count + ' 个任务' : '');
        errors.textContent = data.recovery_issues.length ? '有文件尚未恢复，请查看任务记录并重试。' : '';
        errors.style.display = data.recovery_issues.length ? 'block' : 'none';
        detailTitle.textContent = '任务记录' + (data.needs_attention_count ? ' · ' + data.needs_attention_count + ' 项待查看' : '');
        taskList.replaceChildren();
        data.operations.forEach(operation => {
            const container = document.createElement('div');
            const button = document.createElement('button');
            button.className = 'btn-sm';
            button.textContent = operation.label + ' · ' + (outcomes[operation.result.outcome] || states[operation.state]);
            button.dataset.operationId = operation.id;
            button.addEventListener('click', () => showTask(operation.id));
            container.append(button);
            if (operation.state !== 'queued') {
                const phase = operation.phase;
                container.append(row((phases[phase.name] || phase.name) + '：' + phase.done + (phase.total === null ? ' / 总量尚未确定' : ' / ' + phase.total) + ' ' + (units[phase.unit] || phase.unit) + (phase.detail ? ' · ' + phase.detail : '')));
                const bytes = operation.result.current_item;
                if (bytes) container.append(row('当前文件：' + Math.round(bytes.done / 1024) + (bytes.total === null ? ' / 大小未知' : ' / ' + Math.round(bytes.total / 1024)) + ' KiB'));
            }
            taskList.append(container);
        });
        const selected = data.operations.find(operation => operation.id === selectedId);
        if (selected && details.open && displayedTaskState !== selected.state) showTask(selectedId);
        if (current && current.id === lastRound && lastState !== 'finished' && current.state === 'finished') {
            showToast(outcomes[current.outcome] + '，详情已保存在任务记录', current.outcome.includes('fail') ? 'error' : 'info');
            window.dispatchEvent(new CustomEvent('archive-updated', {detail: current}));
        }
        lastRound = current && current.id;
        lastState = current && current.state;
    }

    async function poll() {
        try {
            const data = await json('/api/progress');
            render(data);
        } catch (error) {
            title.textContent = '暂时无法获取状态';
            message.textContent = '正在重新连接，现有读图位置会保留';
            box.classList.add('status-stale');
            percent.textContent = '';
            bar.parentElement.removeAttribute('aria-valuenow');
        }
        setTimeout(poll, document.hidden ? 5000 : 1500);
    }
    poll();

    function feedback(form, data) {
        let output = form.querySelector('.operation-feedback');
        if (!output) { output = document.createElement('span'); output.className = 'operation-feedback'; form.append(output); }
        output.textContent = '';
        if (data.operation_id) {
            const link = document.createElement('button');
            link.type = 'button'; link.className = 'btn-sm';
            link.textContent = data.created ? '已排队 · 查看任务' : '已有同类任务 · 查看';
            link.addEventListener('click', () => showTask(data.operation_id));
            output.append(link);
        } else output.textContent = data.message || '已完成';
    }

    document.addEventListener('submit', async event => {
        const form = event.target;
        const action = form.getAttribute('action') || '';
        if (form.method.toLowerCase() !== 'post' || !action.startsWith('/') || action.startsWith('//')) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        const confirmation = form.getAttribute('data-confirm');
        if (confirmation && !window.confirm(confirmation)) return;
        const buttons = Array.from(form.querySelectorAll('button[type=submit], button:not([type])'));
        if (buttons.some(button => button.disabled)) return;
        buttons.forEach(button => { button.disabled = true; });
        try {
            const data = await json(action, {method: 'POST', body: new FormData(form)});
            feedback(form, data);
            if (data.mode) {
                const artist = form.closest('.artist-card');
                if (artist) {
                    artist.classList.toggle('inactive', data.mode !== 'active');
                    const toggle = artist.querySelector('form[action$="/toggle"] button');
                    if (toggle) toggle.textContent = data.mode === 'active' ? '暂停同步' : '恢复追踪';
                }
            }
            if (action.endsWith('/hide') || action.endsWith('/unhide')) {
                const hidden = action.endsWith('/hide');
                document.querySelectorAll('form[action$="/hide"] button').forEach(button => { button.style.display = hidden ? 'none' : ''; });
                document.querySelectorAll('form[action$="/unhide"] button').forEach(button => { button.style.display = hidden ? '' : 'none'; });
                document.querySelectorAll('form[data-work-delete]').forEach(form => { form.style.display = hidden ? 'inline' : 'none'; });
            }
        } catch (error) { feedback(form, {message: error.message}); showToast(error.message, 'error'); }
        finally { buttons.forEach(button => { button.disabled = false; }); }
    }, true);

    const missing = document.getElementById('missing-originals');
    if (missing) {
        const container = document.getElementById('missing-originals-list');
        const backfill = document.getElementById('backfill-originals');
        let offset = 0;
        missing.addEventListener('click', async () => {
            try {
                const data = await json('/api/library/missing-originals?offset=' + offset);
                if (!data.items.length && !offset) container.replaceChildren(row('没有已登记的原包缺失项'));
                data.items.forEach(work => {
                    const label = document.createElement('label');
                    label.style.display = 'block';
                    const input = document.createElement('input');
                    input.type = 'checkbox'; input.value = String(work.id);
                    label.append(input, document.createTextNode(' ' + work.title + ' · ' + work.artist_name));
                    container.append(label);
                });
                offset += data.items.length;
                missing.textContent = data.items.length === 100 ? '加载更多缺失项' : '缺失项已列出';
                missing.disabled = data.items.length < 100;
                backfill.hidden = !offset;
            } catch (error) { showToast(error.message, 'error'); }
        });
        backfill.addEventListener('click', async () => {
            const ids = Array.from(container.querySelectorAll('input:checked')).map(input => Number(input.value));
            if (!ids.length) { showToast('先选中需要补下载的作品'); return; }
            try {
                const data = await json('/api/tasks', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({kind:'backfill', work_ids:ids})});
                showTask(data.operation_id);
            } catch (error) { showToast(error.message, 'error'); }
        });
    }
})();
