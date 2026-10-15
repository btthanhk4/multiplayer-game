"""Bot khởi đầu: luôn chọn nước hợp lệ đầu tiên. Không cần thư viện ngoài."""


def choose_move(observation):
    move = observation['legalMoves'][0]
    return {'from': move['from'], 'to': move['to']}
