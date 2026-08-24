const SHANGHAI_DATE_PARTS = new Intl.DateTimeFormat('en-CA', {
	timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
});

export function isValidDateTime(date, time) {
	if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time)) return false;
	const [year, month, day] = date.split('-').map(Number);
	const [hour, minute] = time.split(':').map(Number);
	const value = new Date(Date.UTC(year, month - 1, day, hour, minute));
	return value.getUTCFullYear() === year && value.getUTCMonth() === month - 1 && value.getUTCDate() === day
		&& value.getUTCHours() === hour && value.getUTCMinutes() === minute;
}

function shanghaiYear(referenceTime) {
	const value = new Date(referenceTime);
	if (Number.isNaN(value.getTime())) throw new Error('消息接收时间无效，无法补足消息正文中的年份');
	return Number(SHANGHAI_DATE_PARTS.formatToParts(value).find((part) => part.type === 'year')?.value);
}

function normalizedDateTime(year, month, day, hour, minute) {
	const date = `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
	const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
	return isValidDateTime(date, time) ? { content_date: date, content_time: time } : null;
}

function leadingMessageTime(content, referenceTime) {
	const full = content.match(/^@?(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T]+(\d{1,2}):(\d{2})(?::\d{2})?[ \t]*(?:\r?\n)?/);
	if (full) return { length: full[0].length, value: normalizedDateTime(...full.slice(1, 6).map(Number)) };
	const monthDay = content.match(/^@?(\d{1,2})[-/.](\d{1,2})[ T]+(\d{1,2}):(\d{2})(?::\d{2})?[ \t]*(?:\r?\n)?/);
	if (monthDay) return { length: monthDay[0].length, value: normalizedDateTime(shanghaiYear(referenceTime), ...monthDay.slice(1, 5).map(Number)) };
	const chinese = content.match(/^@?(\d{1,2})月(\d{1,2})日[ T]*(\d{1,2}):(\d{2})(?::\d{2})?[ \t]*(?:\r?\n)?/);
	if (chinese) return { length: chinese[0].length, value: normalizedDateTime(shanghaiYear(referenceTime), ...chinese.slice(1, 5).map(Number)) };
	return null;
}

export function extractImportContent(messageText, { referenceTime = new Date().toISOString() } = {}) {
	const routeTag = String(messageText ?? '').match(/^#([a-z0-9-]+)\s*(?:\r?\n)?/i);
	if (!routeTag) return { content: '' };
	const content = String(messageText).slice(routeTag[0].length).trim();
	const detected = leadingMessageTime(content, referenceTime);
	if (!detected) return { content };
	if (!detected.value) throw new Error('消息正文中的时间无效，请使用 YYYY-MM-DD HH:mm 或 M-D HH:mm');
	return { content: content.slice(detected.length).trim(), ...detected.value };
}
