/**
 * A Feishu delivery is idempotent by source message ID.  Identical attachment
 * bytes are not a duplicate message: analysts can intentionally repost an
 * image, and that new message must still reach the remote archive.
 */
export function shouldSkipMessageForward({ existingJob = null, replayJobId = null }) {
	return Boolean(existingJob && !replayJobId);
}
