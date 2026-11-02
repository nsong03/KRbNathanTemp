// Simulation stand-in for PyRPL's red_pitaya_iir_block.
// The real IIR block contains simulation-only debug code ($fwrite to an undeclared file handle) that
// Icarus Verilog rejects, and the lock does not use the IIR module. This stub keeps the same ports and
// register-bus behaviour (always acknowledges, reads 0) and outputs 0, so red_pitaya_dsp elaborates
// unchanged around it.
`timescale 1ns / 1ps
module red_pitaya_iir_block
#(  parameter IIRBITS = 32,
    parameter IIRSHIFT = 29,
    parameter IIRSTAGES = 14,
    parameter IIRSIGNALBITS = 32,
    parameter SIGNALBITS = 14,
    parameter SIGNALSHIFT = 3,
    parameter LOOPBITS = 10,
    parameter FILTERSTAGES = 1,
    parameter FILTERSHIFTBITS = 3,
    parameter FILTERMINBW = 1000
)
(
    input                 clk_i,
    input                 rstn_i,
    input      [ 14-1: 0] dat_i,
    output reg [ 14-1: 0] dat_o,
    input      [ 16-1: 0] addr,
    input                 wen,
    input                 ren,
    output reg            ack,
    output reg [ 32-1: 0] rdata,
    input      [ 32-1: 0] wdata
);
always @(posedge clk_i) begin
    dat_o <= 14'd0;
    ack   <= wen | ren;
    rdata <= 32'd0;
end
endmodule
