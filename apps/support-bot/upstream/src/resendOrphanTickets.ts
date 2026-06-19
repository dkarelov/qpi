import mongoose from 'mongoose';
import cache from './cache';
import * as db from './db';
import TelegramAddon from './addons/telegram';
import { Context, Messenger } from './interfaces';
import { ISupportee } from './db';
import { sendStaffTicket } from './users';

type ResendMode = 'dry-run' | 'apply';

interface ResendOptions {
  mode: ResendMode;
  ticketIds: number[];
}

function parseTicketId(raw: string): number {
  const normalized = raw.trim().replace(/^#?T/i, '');
  const ticketId = Number(normalized);
  if (!Number.isInteger(ticketId) || ticketId < 1) {
    throw new Error(`invalid ticket id: ${raw}`);
  }
  return ticketId;
}

function parseArgs(argv: string[]): ResendOptions {
  const ticketIds: number[] = [];
  let mode: ResendMode = 'dry-run';

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--apply') {
      mode = 'apply';
      continue;
    }
    if (arg === '--dry-run') {
      mode = 'dry-run';
      continue;
    }
    if (arg === '--ticket-id') {
      const value = argv[index + 1];
      if (!value) {
        throw new Error('--ticket-id requires a value');
      }
      ticketIds.push(parseTicketId(value));
      index += 1;
      continue;
    }
    throw new Error(`unknown argument: ${arg}`);
  }

  return { mode, ticketIds };
}

function buildRecoveryContext(ticket: ISupportee): Context {
  const userId = ticket.userid.toString();
  const firstName = ticket.name || `user ${userId}`;
  const username = ticket.username || '';
  return {
    message: {
      text: '',
      from: {
        id: userId,
        first_name: firstName,
        username,
        is_bot: false,
        language_code: '',
      },
      chat: {
        id: userId,
        first_name: firstName,
        username,
        type: 'private',
      },
      message_id: 0,
      date: 0,
      web_msg: false,
      reply_to_message: {
        from: { is_bot: false },
        text: '',
        caption: '',
      },
      external_reply: { message_id: 0 },
      caption: '',
    },
    chat: {
      id: userId,
      first_name: firstName,
      username,
      type: 'private',
    },
    update_id: 0,
    messenger: ticket.messenger as Messenger,
    session: {
      admin: false,
      mode: null,
      modeData: {
        ticketid: ticket.ticketId.toString(),
        userid: ticket.userid,
        name: firstName,
        category: ticket.category || '',
      },
      pendingSupportContext: ticket.context || null,
      lastContactDate: 0,
      groupCategory: ticket.category,
      groupTag: '',
      group: '',
      groupAdmin: null,
      getSessionKey: () => '',
    },
    callbackQuery: { data: '', from: { id: '' }, id: '' },
    from: { username, id: userId },
    inlineQuery: null,
    answerCbQuery: () => {},
    reply: () => {},
    getChat: () => {},
    getFile: () => {},
  };
}

function recoveryMessage(ticket: ISupportee): string {
  const refs = ticket.context?.refs?.length ? ticket.context.refs.join(', ') : '-';
  return [
    'Служебная повторная отправка обращения.',
    'У этого открытого обращения не было сохраненного сообщения в staff-чате.',
    'Оригинальный текст старого сообщения недоступен в базе, проверьте диалог с пользователем.',
    `Пользователь: ${ticket.name || '-'} ${ticket.username ? `(@${ticket.username})` : ''}`.trim(),
    `Telegram ID: ${ticket.userid}`,
    `Коды: ${refs}`,
  ].join('\n');
}

async function resendOrphanTickets(options: ResendOptions): Promise<ISupportee[]> {
  const tickets = await db.listOpenTicketsWithoutInternalIds(options.ticketIds);
  for (const ticket of tickets) {
    const ticketRef = `T${ticket.ticketId.toString().padStart(6, '0')}`;
    if (options.mode === 'dry-run') {
      console.log(`${ticketRef}\tuser=${ticket.userid}\tcategory=${ticket.category || '-'}`);
      continue;
    }
    const messageId = await sendStaffTicket(ticket, buildRecoveryContext(ticket), undefined, recoveryMessage(ticket));
    await db.addIdAndName(ticket.ticketId, messageId, ticket.name, ticket.username, ticket.context || null);
    console.log(`${ticketRef}\tresent_message_id=${messageId || '-'}`);
  }
  return tickets;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  TelegramAddon.getInstance(cache.config.bot_token);
  await db.connect();
  try {
    const tickets = await resendOrphanTickets(options);
    if (tickets.length === 0) {
      console.log('No open orphan support tickets found.');
    }
  } finally {
    await mongoose.disconnect();
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err?.message || err);
    process.exit(1);
  });
}

export {
  buildRecoveryContext,
  parseArgs,
  parseTicketId,
  recoveryMessage,
  resendOrphanTickets,
};
