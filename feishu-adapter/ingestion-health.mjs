export function isOperatorPausedIngestion(record) {
	return record?.error_class === 'operator_pause';
}
