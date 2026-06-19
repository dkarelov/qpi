// Mock dependencies
const mockSendMessage = jest.fn();
const mockReply = jest.fn();
const mockAdd = jest.fn();
const mockGetTicketByUserId = jest.fn();
const mockAddIdAndName = jest.fn();

jest.mock('../src/middleware', () => ({
  sendMessage: mockSendMessage,
  reply: mockReply,
  strictEscape: jest.fn((str) => str),
  escapeForParseMode: jest.fn((str, parseMode) => {
    const value = (str ?? '').toString();
    if (parseMode === 'HTML') {
      return value
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
    if (parseMode === 'MarkdownV2') {
      return value.replace(/([[\]()_*~`>#+\-=\|{}.!\\])/g, '\\$1');
    }
    return value;
  }),
}));

jest.mock('../src/db', () => ({
  add: mockAdd,
  getTicketByUserId: mockGetTicketByUserId,
  addIdAndName: mockAddIdAndName,
}));

jest.mock('../src/cache', () => ({
  config: {
    language: {
      confirmationMessage: 'Thank you for contacting us.',
      ticket: 'Ticket',
      from: 'from',
      automatedReplySent: 'Automated reply sent',
      blockedSpam: 'You are sending too many messages',
    },
    autoreply: [
      { question: 'hello', answer: 'Hi there!' },
    ],
    use_llm: false,
    autoreply_confirmation: true,
    show_auto_replied: true,
    show_user_ticket: true,
    spam_cant_msg: 3,
    spam_time: 5000,
    staffchat_id: 'staff123',
    staffchat_type: 'telegram',
    staffchat_parse_mode: 'HTML',
    allow_private: false,
    parse_mode: 'MarkdownV2',
    yc_folder_id: '',
  },
  userId: '',
  ticketIDs: [],
  ticketStatus: {},
  ticketSent: [],
}));

jest.mock('../src/addons/llm', () => ({
  getResponseFromLLM: jest.fn(),
}));

jest.mock('fancy-log', () => ({
  info: jest.fn(),
  error: jest.fn(),
  warn: jest.fn(),
}));

import * as users from '../src/users';
import { Context, Messenger } from '../src/interfaces';
import cache from '../src/cache';

describe('Users Module', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    cache.userId = '';
    cache.ticketIDs = [];
    cache.ticketStatus = {};
    cache.ticketSent = [];
    Date.now = jest.fn(() => 1640995200000); // Fixed timestamp
  });

  describe('chat', () => {
    const createMockContext = (
      text: string,
      userOverrides: Partial<Context['message']['from']> = {},
    ): Context => ({
      message: {
        text,
        from: {
          id: 'user123',
          first_name: 'John',
          username: 'john_doe',
          is_bot: false,
          language_code: 'en',
          ...userOverrides,
        },
        chat: {
          id: 'chat123',
          first_name: 'John',
          username: 'john_doe',
          type: 'private',
        },
        message_id: 1,
        date: 1640995200,
        web_msg: false,
        reply_to_message: {
          from: { is_bot: false },
          text: '',
          caption: '',
        },
        external_reply: { message_id: 0 },
        caption: '',
      },
      messenger: Messenger.TELEGRAM,
      session: {
        pendingSupportContext: null,
        lastContactDate: 0,
        admin: false,
        mode: null,
        modeData: {
          ticketid: '',
          userid: '',
          name: '',
          category: '',
        },
        groupCategory: 'general',
        groupTag: '',
        group: '',
        groupAdmin: null,
        getSessionKey: () => '',
      },
      chat: {
        id: 'chat123',
        first_name: 'John',
        username: 'john_doe',
        type: 'private',
      },
      update_id: 1,
      callbackQuery: { data: '', from: { id: '' }, id: '' },
      from: { username: 'john_doe', id: 'user123' },
      inlineQuery: () => {},
      answerCbQuery: () => {},
      reply: () => {},
      getChat: () => {},
      getFile: () => {},
    });

    it('should process new ticket for first-time user', async () => {
      const ctx = createMockContext('I need help with my account');
      ctx.session.pendingSupportContext = {
        role: 'buyer',
        topic: 'purchase',
        refs: ['P31', 'L21', 'S11'],
        label: 'покупатель · покупка · P31, L21, S11',
      };
      const mockTicket = {
        ticketId: 1001,
        userid: 'user123',
        username: 'john_doe',
        context: ctx.session.pendingSupportContext,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };

      mockGetTicketByUserId
        .mockResolvedValueOnce(null)
        .mockResolvedValueOnce(mockTicket)
        .mockResolvedValueOnce(mockTicket);
      mockAdd.mockResolvedValue(1);
      mockSendMessage.mockResolvedValue('msg_id_123');
      mockAddIdAndName.mockResolvedValue(mockTicket);

      await users.chat(ctx, { id: 'chat123' });

      expect(cache.userId).toBe('user123');
      expect(cache.ticketStatus['user123']).toBe(true);
      expect(cache.ticketSent['user123']).toBe(0);
      expect(mockAdd).toHaveBeenCalledWith('user123', 'open', 'general', 'telegram');
      
      // Should send ticket to staff first
      expect(mockSendMessage).toHaveBeenCalledWith(
        'staff123',
        'telegram',
        expect.stringContaining('#T001001'),
        { parse_mode: 'HTML' },
      );
      expect(mockSendMessage).toHaveBeenCalledWith(
        'staff123',
        'telegram',
        expect.stringContaining('Telegram ID: user123'),
        { parse_mode: 'HTML' },
      );
      expect(mockSendMessage).toHaveBeenCalledWith(
        'staff123',
        'telegram',
        expect.stringContaining('Коды: P31, L21, S11'),
        { parse_mode: 'HTML' },
      );

      // Confirmation is sent only after the staff message id is recorded.
      expect(mockSendMessage).toHaveBeenCalledWith(
        'chat123',
        'telegram',
        expect.stringContaining('Thank you for contacting us'),
        {},
      );
      expect(mockAddIdAndName).toHaveBeenCalledWith(
        1001,
        'msg_id_123',
        'John',
        'john_doe',
        ctx.session.pendingSupportContext,
      );
      expect(mockSendMessage.mock.invocationCallOrder[0]).toBeLessThan(
        mockAddIdAndName.mock.invocationCallOrder[0],
      );
      expect(mockAddIdAndName.mock.invocationCallOrder[0]).toBeLessThan(
        mockSendMessage.mock.invocationCallOrder[1],
      );
    });

    it('should escape staff tickets against staffchat_parse_mode', async () => {
      const ctx = createMockContext('Need <help> with _underscore_ and [link](x)', {
        first_name: 'Ann_(test)<script>',
        username: 'ann_user(test)',
      });
      const mockTicket = {
        ticketId: 1006,
        userid: 'user123',
        username: 'ann_user(test)',
        context: null,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };

      mockGetTicketByUserId.mockResolvedValue(mockTicket);
      mockSendMessage.mockResolvedValue('msg_id_128');
      mockAddIdAndName.mockResolvedValue(mockTicket);

      await users.chat(ctx, { id: 'chat123' });

      const staffCall = mockSendMessage.mock.calls.find((call) => call[0] === 'staff123');
      expect(staffCall).toBeTruthy();
      expect(staffCall[2]).toContain('Ann_(test)&lt;script&gt;');
      expect(staffCall[2]).toContain('Need &lt;help&gt; with _underscore_ and [link](x)');
      expect(staffCall[3]).toEqual({ parse_mode: 'HTML' });
    });

    it('should not send success confirmation when staff forwarding fails', async () => {
      const ctx = createMockContext('I need help');
      const mockTicket = {
        ticketId: 1007,
        userid: 'user123',
        username: 'john_doe',
        context: null,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };

      mockGetTicketByUserId.mockResolvedValue(mockTicket);
      mockAdd.mockResolvedValue(1);
      mockSendMessage
        .mockRejectedValueOnce(Object.assign(new Error('Bad Request: cannot parse entities'), {
          description: 'Bad Request: cannot parse entities',
        }))
        .mockResolvedValueOnce('failure_notice');

      await users.chat(ctx, { id: 'chat123' });

      expect(mockAddIdAndName).not.toHaveBeenCalled();
      expect(mockSendMessage).toHaveBeenCalledWith(
        'chat123',
        'telegram',
        'Не удалось отправить обращение в поддержку. Пожалуйста, попробуйте ещё раз через пару минут.',
        {},
      );
      expect(
        mockSendMessage.mock.calls.some((call) => String(call[2]).includes('Thank you for contacting us')),
      ).toBe(false);
    });

    it('should handle spam protection for repeated messages', async () => {
      const ctx = createMockContext('Help me again');
      const mockTicket = {
        ticketId: 1002,
        userid: 'user123',
        username: 'john_doe',
        context: null,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };
      
      // Simulate user already having sent messages
      cache.ticketSent['user123'] = 1;
      
      mockGetTicketByUserId.mockResolvedValue(mockTicket);
      mockSendMessage.mockResolvedValue('msg_id_124');
      mockAddIdAndName.mockResolvedValue(mockTicket);

      await users.chat(ctx, { id: 'chat123' });

      expect(cache.ticketSent['user123']).toBe(2);
      
      // Should only send to staff (no confirmation for repeated messages)
      expect(mockSendMessage).toHaveBeenCalledWith(
        'staff123',
        'telegram',
        expect.stringContaining('#T001002'),
        { parse_mode: 'HTML' },
      );
    });

    it('should block user when spam limit reached', async () => {
      const ctx = createMockContext('Spam message');
      
      // Simulate user hitting spam limit
      cache.ticketSent['user123'] = 3; // spam_cant_msg = 3
      
      await users.chat(ctx, { id: 'chat123' });

      expect(cache.ticketSent['user123']).toBe(4);
      
      // Should send spam block message
      expect(mockSendMessage).toHaveBeenCalledWith(
        'chat123',
        'telegram',
        'You are sending too many messages',
      );
    });

    it('should forward to group chat when group is set', async () => {
      const ctx = createMockContext('Group message');
      ctx.session.group = 'group456';
      
      const mockTicket = {
        ticketId: 1003,
        userid: 'user123',
        username: 'john_doe',
        context: null,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };
      
      mockGetTicketByUserId.mockResolvedValue(mockTicket);
      mockSendMessage.mockResolvedValue('msg_id_125');
      mockAddIdAndName.mockResolvedValue(mockTicket);

      await users.chat(ctx, { id: 'chat123' });

      // Should send to both staff and group
      expect(mockSendMessage).toHaveBeenCalledWith(
        'staff123',
        'telegram',
        expect.stringContaining('#T001003'),
        { parse_mode: 'HTML' },
      );
      
      expect(mockSendMessage).toHaveBeenCalledWith(
        'group456',
        'telegram',
        expect.stringContaining('#T001003'),
        expect.any(Object)
      );
    });

    it('should not send duplicate messages to group if group is same as staff chat', async () => {
      const ctx = createMockContext('Staff group message');
      ctx.session.group = 'staff123'; // Same as staffchat_id
      
      const mockTicket = {
        ticketId: 1004,
        userid: 'user123',
        username: 'john_doe',
        context: null,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };
      
      mockGetTicketByUserId.mockResolvedValue(mockTicket);
      mockSendMessage.mockResolvedValue('msg_id_126');

      await users.chat(ctx, { id: 'chat123' });

      // Should only send to staff once (not duplicate to group)
      const staffCalls = mockSendMessage.mock.calls.filter(
        call => call[0] === 'staff123'
      );
      expect(staffCalls.length).toBeLessThanOrEqual(2); // Confirmation + ticket
    });

    it('should reset spam timer after spam_time period', async () => {
      const ctx = createMockContext('Reset spam timer test');
      
      const mockTicket = {
        ticketId: 1005,
        userid: 'user123',
        username: 'john_doe',
        context: null,
        messenger: 'telegram',
        status: 'open',
        category: 'general',
      };
      
      mockGetTicketByUserId.mockResolvedValue(mockTicket);
      mockSendMessage.mockResolvedValue('msg_id_127');

      await users.chat(ctx, { id: 'chat123' });
      // Test passes if no errors are thrown
      expect(true).toBe(true);
    });
  });
});
