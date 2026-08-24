export async function writeChunk(writer, bytes) {
	await new Promise((resolve, reject) => {
		let settled = false;
		const cleanup = () => {
			writer.off('drain', onDrain);
			writer.off('error', onError);
		};
		const finish = (callback, value) => {
			if (settled) return;
			settled = true;
			cleanup();
			callback(value);
		};
		const onDrain = () => finish(resolve);
		const onError = (error) => finish(reject, error);

		writer.once('error', onError);
		try {
			if (writer.write(bytes)) finish(resolve);
			else writer.once('drain', onDrain);
		} catch (error) {
			finish(reject, error);
		}
	});
}

export async function endWritable(writer) {
	await new Promise((resolve, reject) => {
		let settled = false;
		const cleanup = () => writer.off('error', onError);
		const finish = (callback, value) => {
			if (settled) return;
			settled = true;
			cleanup();
			callback(value);
		};
		const onError = (error) => finish(reject, error);
		writer.once('error', onError);
		try {
			writer.end((error) => error ? finish(reject, error) : finish(resolve));
		} catch (error) {
			finish(reject, error);
		}
	});
}
