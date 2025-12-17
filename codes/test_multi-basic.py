import asyncio

from metagpt.actions import Action
from metagpt.environment import Environment
from metagpt.roles import Role
from metagpt.team import Team

action1 = Action(name="AlexSay", instruction="Given the word from Cindy, describe the word to Bob without saying directly out.")
action2 = Action(name="BobSay", instruction="Given the words from Alex ,guess the word and ask questions to Alex.")
action3=Action(name="CindySay",instruction="Offer a word to Alex.Stop the round when Bob say the right word.")
alex = Role(name="Alex", profile="Word describer", goal="Describe the word", actions=[action1], watch=[action2,action3])
bob = Role(name="Bob", profile="Word guesser", goal="Guess the right word", actions=[action2], watch=[action1])
cindy = Role(name="Cindy", profile="Word provider", goal="Give the word to Alex", actions=[action3], watch=[action2,action1])
env = Environment(desc="Guess the word")
team = Team(investment=10.0, env=env, roles=[alex, bob,cindy])

asyncio.run(team.run(idea="Cindy prepares a simple word and tell Alex.Alex descirbes the word to Bob.When Cindy hears Bob say the right word,stop one round", send_to="Cindy", n_round=5))
