import praw
from bitcoin.base58 import decode
import calendar
import datetime
from decimal import Decimal
import hashlib
import os.path
from StringIO import StringIO
import yaml
import sys

import error

from bitcoin.core import *
from bitcoin.core.script import *

def assert_tx2_valid(tx2):
  """
  Checks the TX2 provided by the remote peer matches the expected structure.
  Raises an exception in case of a problem
  """

  lock_min_datetime = datetime.datetime.utcnow() + datetime.timedelta(hours=12)
  lock_max_datetime = datetime.datetime.utcnow() + datetime.timedelta(hours=72)
  lock_time = tx2.nLockTime
  if lock_time < calendar.timegm(lock_min_datetime.timetuple()):
    raise error.TradeError("TX2 lock time is "
      + datetime.datetime.utcfromtimestamp(lock_time).strftime('%Y-%m-%d %H:%M:%S')
      + " which is less than 24 hours in the future.")
  if lock_time > calendar.timegm(lock_max_datetime.timetuple()):
    raise error.TradeError("TX2 lock time is "
      + datetime.datetime.utcfromtimestamp(lock_time).strftime('%Y-%m-%d %H:%M:%S')
      + " which is more than 72 hours in the future.")

  if len(tx2.vin) != 1:
    raise error.TradeError("TX2 does not have exactly one input.")
  if len(tx2.vout) != 1:
    raise error.TradeError("TX2 does not exactly one output.")

  # TODO: Check the output value is close to the trade total (i.e. about right
  # after fees have been deducted

  # If the nSequence is 0xffffffff, the transaction can be considered valid
  # despite what the lock time says. Current implementations do not support this,
  # however we want to be sure anyway
  if tx2.vin[0].nSequence == 0xffffffff:
    raise error.TradeError("TX2 input's sequence is final; must be less than MAX_INT.")

def find_inputs(proxy, quantity):
  """ Find unspent outputs equal to or greater than the given target
      quantity.

      quantity is the number of coins to be sent, expressed as an integer quantity of the smallest
      unit (i.e. Satoshi)

      returns a tuple of an array of CMutableTxIn and the total input value
  """

  total_in = 0
  txins = []

  for txout in proxy.listunspent(0):
    total_in += txout['amount']
    txins.append(CMutableTxIn(txout['outpoint']))
    if total_in >= quantity:
      break

  if total_in < quantity:
    raise error.FundsError('Insufficient funds.')

  return (txins, total_in)

def build_tx1_cscript(own_address, peer_address, secret_hash):
  """
  Generates the script for TX1/TX3's main output (i.e. not the change)
  """

  # scriptSig is either:
  #     0 <signature B> <signature A> 2 <A public key> <B public key> 2
  # or
  #     <shared secret> <signature B> <B public key> 0
  return CScript(
    [
      OP_IF,
        OP_2DUP, # Multisig
          OP_HASH160, peer_address, OP_EQUALVERIFY,
          OP_HASH160, own_address, OP_EQUALVERIFY,
          2, OP_CHECKMULTISIG,
      OP_ELSE,
        OP_DUP, # Single sig + hash
          OP_HASH160, peer_address, OP_EQUALVERIFY,
          OP_CHECKSIGVERIFY,
          OP_HASH256, b2x(secret_hash), OP_EQUAL,
      OP_ENDIF
    ]
  )

def build_tx1(proxy, quantity, own_address, peer_address, secret_hash, fee_rate):
  """
  Generates "TX1" from the guide at https://en.bitcoin.it/wiki/Atomic_cross-chain_trading
  Pay w BTC to <B's public key> if (x for H(x) known and signed by B) or (signed by A & B)

  proxy is the RPC proxy to the relevant daemon JSON-RPC interface
  quantity is the number of coins to be sent, expressed as an integer quantity of the smallest
    unit (i.e. Satoshi)
  peer_public_key is the public key with which the payment and refund transactions must be signed, expressed as CBase58Data
  own_public_key is the public key this client signs the refund transaction with, expressed as CBase58Data
  secret_hash the secret value passed through SHA256 twice

  returns a CTransaction
  """
  # TODO: Use actual transaction size once we have a good estimate
  quantity_inc_fee = quantity + fee_rate.get_fee(2000)
  (txins, total_in) = find_inputs(proxy, quantity_inc_fee)

  txout = CTxOut(quantity, build_tx1_cscript(own_address, peer_address, secret_hash))
  txouts = [txout]

  # Generate a change transaction if needed
  if total_in > quantity_inc_fee:
    change = total_in - quantity_inc_fee
    change_address = proxy.getrawchangeaddress()
    change_txout = CTxOut(change, change_address.to_scriptPubKey())
    txouts.append(change_txout)

  tx = CMutableTransaction(txins, txouts)
  tx_signed = proxy.signrawtransaction(tx)
  if not tx_signed['complete']:
      raise error.TradeError('Transaction came back without all inputs signed.')

  # TODO: Lock outputs which are used by this transaction

  return tx_signed['tx']

def build_tx2(proxy, tx1, nLockTime, own_address, fee_rate):
  """
  Generates "TX2" from the guide at https://en.bitcoin.it/wiki/Atomic_cross-chain_trading.
  The same code can also generate TX4. These are the refund transactions in case of
  problems. Transaction outputs are locked to the script:
        Pay w BTC from TX1 to <A's public key>, locked 48 hours in the future, signed by A

  proxy is the RPC proxy to the relevant daemon JSON-RPC interface
  tx1 the (complete, signed) transaction to refund from
  nLockTime the lock time to set on the transaction
  own_address is the private address this client signs the refund transaction with. This must match
      the address provided when generating TX1.

  returns a CMutableTransaction
  """
  prev_txid = tx1.GetHash()
  prev_out = tx1.vout[0]
  txin = CMutableTxIn(COutPoint(prev_txid, 0), nSequence=1)

  seckey = proxy.dumpprivkey(own_address)

  txin_scriptPubKey = prev_out.scriptPubKey

  fee = fee_rate.get_fee(1000)
  txouts = [CTxOut(prev_out.nValue - fee, own_address.to_scriptPubKey())]

  # Create the unsigned transaction
  tx = CMutableTransaction([txin], txouts, nLockTime)

  # Calculate the signature hash for the transaction.
  sighash = SignatureHash(txin_scriptPubKey, tx, 0, SIGHASH_ALL)

  # Now sign it. We have to append the type of signature we want to the end, in
  # this case the usual SIGHASH_ALL.
  sig = seckey.sign(sighash) + bytes([SIGHASH_ALL])

  # scriptSig needs to be:
  #     0 <signature B> <signature A> 2 <A public key> <B public key> 2
  # However, we only can do one side, so we leave the rest to the other side to
  # complete
  txin.scriptSig = CScript([sig, seckey.pub])

  return tx

def build_tx3(proxy, quantity, own_address, peer_address, secret_hash, fee_rate):
  return build_tx1(proxy, quantity, peer_address, own_address, secret_hash, fee_rate)

def build_tx4(proxy, tx3, nLockTime, own_address, fee_rate):
  return build_tx2(proxy, tx3, nLockTime, own_address, fee_rate)


"""
Generate the spending transaction for TX3/TX1.

seckey is the secret key used to sign the transaction
"""
def build_tx3_spend(proxy, tx1, secret, own_address):
  fee = fee_rate.get_fee(1000)

  prev_txid = tx1.GetHash()
  prev_out = tx1.vout[0]
  txin = CMutableTxIn(COutPoint(prev_txid, 0), nSequence=1)
  txins = [txin]

  txin_scriptPubKey = prev_out.scriptPubKey

  txouts = [CTxOut(prev_out.nValue - fee, own_address.to_scriptPubKey())]

  # Create the unsigned transaction
  tx = CMutableTransaction(txins, txouts)

  # Calculate the signature hash for that transaction.
  sighash = SignatureHash(txin_scriptPubKey, tx, 0, SIGHASH_ALL)

  # Now sign it. We have to append the type of signature we want to the end, in
  # this case the usual SIGHASH_ALL.
  sig = seckey.sign(sighash) + bytes([SIGHASH_ALL])

  # scriptSig needs to be:
  #     <shared secret> <signature B> <B public key> 0
  txin.scriptSig = CScript([secret, sig, seckey.pub, 0])

  return tx

def sign_tx2(proxy, tx2, own_address, peer_address, secret_hash):
  if len(tx2.vin) != 1:
    raise error.TradeError("TX2 does not have exactly one input")

  other_sig = None
  other_pubkey = None
  for (opcode, data, sop_idx) in tx2.vin[0].scriptSig.raw_iter():
    if other_sig == None:
      other_sig = data
    elif other_pubkey == None:
      other_pubkey = data
    else:
      raise error.TradeError("TX2 input has more than two elements, expected exactly two.")
  if not other_sig or not other_pubkey:
      raise error.TradeError("TX2 input has less than two elements, expected exactly two.")

  seckey = proxy.dumpprivkey(own_address)

  # Rebuild the input script for TX1
  # Note the inputs are reversed because we're creating it from the peer's point of view
  txin_scriptPubKey = build_tx1_cscript(peer_address, own_address, secret_hash)

  # Calculate the signature hash for the transaction.
  sighash = SignatureHash(txin_scriptPubKey, tx2, 0, SIGHASH_ALL)

  # Now sign it. We have to append the type of signature we want to the end, in
  # this case the usual SIGHASH_ALL.
  sig = seckey.sign(sighash) + bytes([SIGHASH_ALL])

  # Create a mutable version
  tx2 = CMutableTransaction.from_tx(tx2)

  # scriptSig needs to be:
  #     0 <signature B> <signature A> 2 <A public key> <B public key> 2
  tx2.vin[0].scriptSig = CScript([0, sig, other_sig, seckey.pub, other_pubkey, 2])

  return tx2