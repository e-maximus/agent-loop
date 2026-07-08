// Imported first by run-issue.ts so these run before config.ts loads.
// The autofix path doesn't use Telegram, but config always requires the
// Telegram vars — provide harmless defaults so validation passes.
import 'dotenv/config';
process.env.TELEGRAM_BOT_TOKEN ||= 'unused-for-run-issue';
process.env.TELEGRAM_ALLOWED_USER_IDS ||= '0';
