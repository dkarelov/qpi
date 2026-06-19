import { Context, Messenger, ParseMode, SupportContext } from './interfaces';
import cache from './cache';
import * as llm from './addons/llm';
import * as db from './db';
import { escapeForParseMode, strictEscape as esc, reply, sendMessage } from './middleware';
import { ISupportee } from './db';
import { formatSupportContextSummary, formatSupportRoleLabel } from './supportContext';
import { recordSupportMetric } from './metrics';
import * as log from 'fancy-log';

const TIME_BETWEEN_CONFIRMATION_MESSAGES = 86400000; // 24 hours
const STAFF_FORWARD_ATTEMPT_METRIC = 'qpi.support.ticket.staff_forward_attempt';
const STAFF_FORWARD_FAILURE_METRIC = 'qpi.support.ticket.staff_forward_failure';
const USER_CONFIRMATION_SENT_METRIC = 'qpi.support.ticket.user_confirmation_sent';
const TEMPORARY_SUPPORT_FAILURE_MESSAGE =
  'Не удалось отправить обращение в поддержку. Пожалуйста, попробуйте ещё раз через пару минут.';

function normalizeStaffChatParseMode(): ParseMode {
  const parseMode = cache.config.staffchat_parse_mode || ParseMode.HTML;
  if (parseMode === ParseMode.Markdown) {
    return ParseMode.MarkdownV2;
  }
  return parseMode;
}

function ticketMetricLabels(ticket: ISupportee): Record<string, string> {
  return {
    ticket_id: ticket.ticketId.toString(),
    ticket_ref: `T${ticket.ticketId.toString().padStart(6, '0')}`,
    messenger: ticket.messenger,
    category: ticket.category || '',
  };
}

function safeErrorDescription(err: any): string {
  return String(err?.description || err?.message || err || '').slice(0, 300);
}

/**
 * Generates a ticket message.
 *
 * @param ticket - Ticket object with a toString() method.
 * @param message - Message object containing text and sender info.
 * @param tag - Tag string.
 * @param anonymousUser - Whether the ticket is anonymous (default: true).
 * @param autoReplyInfo - Optional auto-reply info to append.
 * @returns The formatted ticket message.
 */
function formatMessageAsTicket(
  ticket: ISupportee | { toString: () => string },
  ctx: Context,
  autoReplyInfo?: any,
  messageText?: string,
): string {
  const { config } = cache;
  const parseMode = normalizeStaffChatParseMode();
  const name = ctx.message.from.first_name;
  const ticketId = typeof (ticket as any).ticketId !== 'undefined'
    ? (ticket as ISupportee).ticketId
    : ticket.toString();
  const username = ctx.message.from.username || (ticket as ISupportee).username || '';
  const context = resolveSupportContext(ctx, ticket as ISupportee);
  const metadataLines = [
    `Telegram ID: ${ctx.message.from.id.toString()}`,
    `Username: ${username ? '@' + username.replace(/^@/, '') : '-'}`,
  ];
  if (context) {
    metadataLines.push(`Роль: ${formatSupportRoleLabel(context.role)}`);
    metadataLines.push(`Контекст: ${context.label}`);
    if (context.refs.length > 0) {
      metadataLines.push(`Коды: ${context.refs.join(', ')}`);
    }
  }
  const body = messageText !== undefined ? messageText : ctx.message.text;
  const header = `${config.language.ticket} #T${ticketId
    .toString()
    .padStart(6, '0')} ${config.language.from} ${name} ${ctx.session.groupTag}`;
  const escapedBlocks = [
    escapeForParseMode(header, parseMode),
    escapeForParseMode(metadataLines.join('\n'), parseMode),
    escapeForParseMode(body || '', parseMode),
  ];
  if (autoReplyInfo) {
    const escapedAutoReplyInfo = escapeForParseMode(autoReplyInfo, parseMode);
    escapedBlocks.push(
      parseMode === ParseMode.HTML
        ? `<i>${escapedAutoReplyInfo}</i>`
        : parseMode === ParseMode.MarkdownV2
          ? `*${escapedAutoReplyInfo}*`
          : escapedAutoReplyInfo,
    );
  }
  return escapedBlocks.filter((block) => block !== '').join('\n\n');
}

function resolveSupportContext(ctx: Context, ticket?: ISupportee | null): SupportContext | null {
  return ctx.session.pendingSupportContext || ticket?.context || null;
}

/**
 * Creates a formatted auto-reply ticket message.
 *
 * @param msg - The auto-reply message content.
 * @param ctx - Bot context.
 * @returns The formatted auto-reply message.
 */
function createAutoReplyMessage(msg: string, ctx: Context): string {
  const { config } = cache;
  const senderName = ctx.message.from.first_name;
  return config.clean_replies
    ? msg
    : `${config.language.dear} ${esc(senderName)},\n\n${msg}\n\n${config.language.regards}\n${config.language.automatedReplyAuthor}\n\n*${config.language.automatedReply}*`;
}

/**
 * Checks for common questions and LLM responses to auto-reply.
 *
 * @param ctx - Bot context.
 * @returns True if an auto-reply was sent; otherwise, false.
 */
async function autoReply(ctx: Context): Promise<boolean> {
  const {
    config: { autoreply, use_llm },
  } = cache;
  const messageText = ctx.message.text.toString();

  if (autoreply && autoreply.length > 0 && autoreply[0]?.question) {
    // Check common auto-reply questions
    for (const autoReplyItem of autoreply) {
      if (messageText.includes(autoReplyItem.question)) {
        reply(ctx, createAutoReplyMessage(autoReplyItem.answer, ctx));
        return true;
      }
    }
  }

  // Fallback to LLM response if enabled
  if (use_llm) {
    const response = await llm.getResponseFromLLM(ctx);
    if (response !== null) {
      reply(ctx, createAutoReplyMessage(response, ctx));
      return true;
    }
  }
  return false;
}

/**
 * Processes a ticket by forwarding it to staff, recording the staff message id, and then confirming to the user.
 *
 * @param ticket - The ticket retrieved from the database.
 * @param ctx - Bot context.
 * @param chatId - The chat id for sending confirmation.
 * @param autoReplyInfo - Optional auto-reply info.
 */
async function processTicket(
  ticket: ISupportee,
  ctx: Context,
  chatId: string,
  autoReplyInfo?: string,
) {
  const { config } = cache;
  const supportContext = resolveSupportContext(ctx, ticket);
  const shouldConfirmUser = !autoReplyInfo &&
    config.autoreply_confirmation &&
    (ctx.session.lastContactDate === undefined ||
      ctx.session.lastContactDate < Date.now() - TIME_BETWEEN_CONFIRMATION_MESSAGES);

  let staffMessageId: string | null = null;
  try {
    staffMessageId = await sendStaffTicket(ticket, ctx, autoReplyInfo);
    await db.addIdAndName(
      ticket.ticketId,
      staffMessageId,
      ctx.message.from.first_name,
      ctx.message.from.username,
      supportContext,
    );
  } catch (err) {
    recordSupportMetric(
      STAFF_FORWARD_FAILURE_METRIC,
      {
        ...ticketMetricLabels(ticket),
        error_type: err?.constructor?.name || 'Error',
      },
    );
    log.error('support_ticket_staff_forward_failed', {
      ticket_id: ticket.ticketId,
      user_id: ticket.userid,
      error_type: err?.constructor?.name || 'Error',
      telegram_error_description: safeErrorDescription(err),
    });
    await sendMessage(chatId, ticket.messenger, TEMPORARY_SUPPORT_FAILURE_MESSAGE, {});
    return;
  }

  if (
    shouldConfirmUser
  ) {
    ctx.session.lastContactDate = Date.now();
    const confirmationMsg =
      config.language.confirmationMessage +
      '\n' +
      (config.show_user_ticket
        ? `${config.language.ticket} #T${ticket.ticketId.toString().padStart(6, '0')}`
        : '') +
      (formatSupportContextSummary(supportContext)
        ? `\n${formatSupportContextSummary(supportContext)}`
        : '');
    await sendMessage(chatId, ticket.messenger, confirmationMsg, {});
    recordSupportMetric(USER_CONFIRMATION_SENT_METRIC, ticketMetricLabels(ticket));
    log.info('support_ticket_user_confirmation_sent', {
      ticket_id: ticket.ticketId,
      user_id: ticket.userid,
      staff_message_id: staffMessageId,
    });
  }

  // If group flag is set and not the admin chat, forward to group chat
  if (ctx.session.group && ctx.session.group !== config.staffchat_id) {
    const groupOptions = config.allow_private
      ? {
        parse_mode: 'none',
        reply_markup: {
          html: '',
          inline_keyboard: [
            [
              {
                text: config.language.replyPrivate,
                callback_data:
                  ctx.from.id +
                  '---' +
                  ctx.message.from.first_name +
                  '---' +
                  ctx.session.groupCategory +
                  '---' +
                  ticket.ticketId,
              },
            ],
          ],
        },
      }
      : { parse_mode: config.parse_mode };

    sendMessage(
      ctx.session.group,
      ticket.messenger,
      formatMessageAsTicket(
        ticket,
        ctx,
        autoReplyInfo,
      ),
      groupOptions,
    );
  }
};

async function sendStaffTicket(
  ticket: ISupportee,
  ctx: Context,
  autoReplyInfo?: string,
  messageText?: string,
): Promise<string | null> {
  const { config } = cache;
  const parseMode = normalizeStaffChatParseMode();
  recordSupportMetric(STAFF_FORWARD_ATTEMPT_METRIC, ticketMetricLabels(ticket));
  const staffMessageId = await sendMessage(
    config.staffchat_id,
    config.staffchat_type,
    formatMessageAsTicket(
      ticket,
      ctx,
      autoReplyInfo,
      messageText,
    ),
    parseMode === ParseMode.PLAINTEXT ? {} : { parse_mode: parseMode },
  );
  if (config.staffchat_type === Messenger.TELEGRAM && !staffMessageId) {
    throw new Error('staff ticket send did not return a Telegram message id');
  }
  return staffMessageId;
}

async function ensureOpenTicket(ctx: Context, userId: string | number): Promise<ISupportee> {
  let ticket = await db.getTicketByUserId(userId, ctx.session.groupCategory);
  if (ticket) {
    return ticket;
  }

  await db.add(userId, 'open', ctx.session.groupCategory, ctx.messenger);
  ticket = await db.getTicketByUserId(userId, ctx.session.groupCategory);
  if (!ticket) {
    throw new Error(`Failed to create or load ticket for user ${userId}`);
  }

  return ticket;
}

/**
 * Handles ticket processing with spam protection.
 *
 * @param ctx - Bot context.
 * @param chat - Chat object containing an id.
 */
async function chat(ctx: Context, chat: { id: string }) {
  const { config } = cache;
  cache.userId = ctx.message.from.id;
  const isAutoReply = await autoReply(ctx);
  if (isAutoReply && !config.show_auto_replied) return;
  const autoReplyInfo = isAutoReply ? config.language.automatedReplySent : undefined;

  // Ensure the user's ticket is tracked
  if (cache.ticketIDs[cache.userId] === undefined) {
    cache.ticketIDs.push(cache.userId);
  }
  cache.ticketStatus[cache.userId] = true;

  // If no ticket has been sent yet, fetch from DB and set up spam timer
  if (cache.ticketSent[cache.userId] === undefined) {
    const ticket = await ensureOpenTicket(ctx, cache.userId);
    await processTicket(ticket, ctx, chat.id, autoReplyInfo);

    // Prevent multiple notifications for a period defined by spam_time
    setTimeout(() => {
      cache.ticketSent[cache.userId] = undefined;
    }, config.spam_time);
    cache.ticketSent[cache.userId] = 0;
  } else if (cache.ticketSent[cache.userId] < config.spam_cant_msg) {
    cache.ticketSent[cache.userId]++;
    const ticket = await ensureOpenTicket(ctx, cache.userId);
    try {
      const staffMessageId = await sendStaffTicket(ticket, ctx, autoReplyInfo);
      await db.addIdAndName(
        ticket.ticketId,
        staffMessageId,
        ctx.message.from.first_name,
        ctx.message.from.username,
        resolveSupportContext(ctx, ticket),
      );
    } catch (err) {
      recordSupportMetric(
        STAFF_FORWARD_FAILURE_METRIC,
        {
          ...ticketMetricLabels(ticket),
          error_type: err?.constructor?.name || 'Error',
        },
      );
      log.error('support_ticket_staff_forward_failed', {
        ticket_id: ticket.ticketId,
        user_id: ticket.userid,
        error_type: err?.constructor?.name || 'Error',
        telegram_error_description: safeErrorDescription(err),
      });
      await sendMessage(chat.id, ticket.messenger, TEMPORARY_SUPPORT_FAILURE_MESSAGE, {});
      return;
    }
    if (ctx.session.group && ctx.session.group !== config.staffchat_id) {
      sendMessage(
        ctx.session.group,
        ticket.messenger,
        formatMessageAsTicket(
          ticket,
          ctx,
          autoReplyInfo,
        ),
      );
    }
  } else if (cache.ticketSent[cache.userId] === config.spam_cant_msg) {
    cache.ticketSent[cache.userId]++;
    sendMessage(chat.id, ctx.messenger, config.language.blockedSpam);
  }

  // Log the ticket message for debugging
  const ticket = await ensureOpenTicket(ctx, cache.userId);
  log.info('support_ticket_processed', {
    ticket_id: ticket.ticketId,
    user_id: ticket.userid,
    category: ticket.category,
    auto_reply: Boolean(autoReplyInfo),
  });
}

export {
  chat,
  formatMessageAsTicket,
  normalizeStaffChatParseMode,
  sendStaffTicket,
  TEMPORARY_SUPPORT_FAILURE_MESSAGE,
};
