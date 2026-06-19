const mockListOpenTicketsWithoutInternalIds = jest.fn();
const mockAddIdAndName = jest.fn();
const mockSendStaffTicket = jest.fn();

jest.mock('../src/db', () => ({
  listOpenTicketsWithoutInternalIds: mockListOpenTicketsWithoutInternalIds,
  addIdAndName: mockAddIdAndName,
}));

jest.mock('../src/users', () => ({
  sendStaffTicket: mockSendStaffTicket,
}));

jest.mock('../src/cache', () => ({
  config: {
    bot_token: 'token',
    language: {
      ticket: 'Обращение',
      from: 'от',
    },
    staffchat_parse_mode: 'HTML',
  },
}));

import {
  parseArgs,
  parseTicketId,
  recoveryMessage,
  resendOrphanTickets,
} from '../src/resendOrphanTickets';

describe('resend orphan tickets script', () => {
  const ticket = {
    ticketId: 6,
    userid: '12345',
    internalIds: [],
    name: 'Tatiana',
    username: 'tatiana_support',
    context: {
      role: 'buyer',
      topic: 'purchase',
      refs: ['P12'],
      label: 'покупатель · покупка · P12',
    },
    messenger: 'telegram',
    status: 'open',
    category: null,
  };

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('parses ticket refs and defaults to dry-run', () => {
    expect(parseTicketId('T000006')).toBe(6);
    expect(parseTicketId('#T12')).toBe(12);
    expect(parseArgs(['--ticket-id', 'T000006'])).toEqual({
      mode: 'dry-run',
      ticketIds: [6],
    });
    expect(parseArgs(['--apply', '--ticket-id', '6'])).toEqual({
      mode: 'apply',
      ticketIds: [6],
    });
  });

  it('dry-run lists open orphan tickets without sending them', async () => {
    mockListOpenTicketsWithoutInternalIds.mockResolvedValue([ticket]);
    const logSpy = jest.spyOn(console, 'log').mockImplementation(() => undefined);

    await resendOrphanTickets({ mode: 'dry-run', ticketIds: [6] });

    expect(mockListOpenTicketsWithoutInternalIds).toHaveBeenCalledWith([6]);
    expect(mockSendStaffTicket).not.toHaveBeenCalled();
    expect(mockAddIdAndName).not.toHaveBeenCalled();
    expect(logSpy).toHaveBeenCalledWith('T000006\tuser=12345\tcategory=-');
    logSpy.mockRestore();
  });

  it('apply resends staff ticket and records the returned internal id', async () => {
    mockListOpenTicketsWithoutInternalIds.mockResolvedValue([ticket]);
    mockSendStaffTicket.mockResolvedValue('777');
    const logSpy = jest.spyOn(console, 'log').mockImplementation(() => undefined);

    await resendOrphanTickets({ mode: 'apply', ticketIds: [6] });

    expect(mockSendStaffTicket).toHaveBeenCalledWith(
      ticket,
      expect.objectContaining({
        session: expect.objectContaining({
          pendingSupportContext: ticket.context,
        }),
      }),
      undefined,
      expect.stringContaining('Оригинальный текст старого сообщения недоступен'),
    );
    expect(mockAddIdAndName).toHaveBeenCalledWith(6, '777', 'Tatiana', 'tatiana_support', ticket.context);
    expect(logSpy).toHaveBeenCalledWith('T000006\tresent_message_id=777');
    logSpy.mockRestore();
  });

  it('recovery message includes refs and avoids pretending the original body exists', () => {
    const message = recoveryMessage(ticket as any);

    expect(message).toContain('Коды: P12');
    expect(message).toContain('Оригинальный текст старого сообщения недоступен');
  });
});
