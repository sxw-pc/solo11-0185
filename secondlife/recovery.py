from .scale import net

def total(tickets):
    return sum(net(ticket) for ticket in tickets)
