import readline from 'node:readline';
import {newGame, applyMove, legalMoves, adjudicated} from '../rules.js';
let state = newGame();
function snapshot() {
  const moves = [];
  if (!state.winner) for (let r=0;r<9;r++) for (let c=0;c<9;c++) {
    if (state.board[r][c]?.owner === state.turn) for (const m of legalMoves(state.board,r,c)) {
      moves.push({from:[r,c],to:[m.row,m.col],capture:m.kind==='capture'});
    }
  }
  return {state,legalMoves:moves};
}
for await (const line of readline.createInterface({input:process.stdin})) {
  try {
    const command = JSON.parse(line);
    if (command.op === 'new') state = newGame(command.map || 'standard');
    else if (command.op === 'move') {
      const result = applyMove(state,state.turn,command.from,command.to,{adjudicate:true});
      if (!result.ok) throw new Error(result.error);
      state = result.state;
    } else if (command.op === 'adjudicate') {
      // The arena ends a game it cannot continue (move cap, no legal move) the same way the rules end a stalemate.
      process.stdout.write(JSON.stringify({ok:true,verdict:adjudicated(state,String(command.cause)),...snapshot()})+'\n');
      continue;
    } else throw new Error('Unknown referee operation');
    process.stdout.write(JSON.stringify({ok:true,...snapshot()})+'\n');
  } catch (error) { process.stdout.write(JSON.stringify({ok:false,error:error.message})+'\n'); }
}
