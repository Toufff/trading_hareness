export function singleFlight(task) {
	let active = null;
	return async (...args) => {
		if (active) return active;
		active = Promise.resolve().then(() => task(...args)).finally(() => { active = null; });
		return active;
	};
}
