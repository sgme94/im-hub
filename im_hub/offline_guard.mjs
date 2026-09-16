// Trial-only process network guard. No UI/server/cloud AI is needed for local CLI tests.
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import dgram from 'node:dgram';
import { syncBuiltinESMExports } from 'node:module';
const denied = () => { throw new Error('TRIAL_NETWORK_DISABLED'); };
net.Socket.prototype.connect = denied;
net.connect = denied;
net.createConnection = denied;
tls.connect = denied;
http.request = denied;
http.get = denied;
https.request = denied;
https.get = denied;
dgram.createSocket = denied;
globalThis.fetch = async () => { throw new Error('TRIAL_NETWORK_DISABLED'); };
syncBuiltinESMExports();
